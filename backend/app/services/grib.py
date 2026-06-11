"""Parse NOAA GRIB2 files into 10m wind grids on regular lat/lon grids.

GFS arrives on a regular grid (1D lat/lon coords) and is used as-is.
HRRR arrives on a Lambert Conformal Conic grid (2D coords) and is
regridded here so all downstream consumers see the same shape.

Regridding performance
----------------------
``scipy.interpolate.griddata`` rebuilds a Delaunay triangulation of the
source points on EVERY call — ~1.9M points for conus HRRR, twice per
fhour (u, v) plus nearest-neighbour passes for NaN fill. That cost made
the conus ingest job CPU-bound at ~60 s/fhour and pushed full-cycle runs
into the Cloud Run task timeout.

The source LCC grid is identical in every HRRR file and the target grid
is fixed per (region, resolution), so the geometry work is done ONCE per
process and cached: triangulate, locate each target point's simplex,
precompute barycentric weights, and precompute nearest-source indices
for points outside the convex hull. Per fhour, regridding is then a
pure-numpy gather + weighted sum (sub-second). The numerics are the
same linear barycentric interpolation + nearest fill ``griddata``
performs — outputs are equivalent, no ENGINE_VERSION implications.

Only the small index/weight arrays are retained (a few MB); the
Delaunay object and KD-tree are released after the build, so steady-
state memory matches the old per-call approach.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import xarray as xr


@dataclass(frozen=True)
class WindGrid:
    """10m above-ground wind components on a REGULAR lat/lon grid.

    Conventions:
      lats: 1D, degrees, sorted (any direction)
      lons: 1D, degrees, sorted ascending in -180..180
      u, v: 2D shape (len(lats), len(lons)), m/s — eastward / northward
    """
    lats: np.ndarray
    lons: np.ndarray
    u: np.ndarray
    v: np.ndarray
    reference_time: datetime
    valid_time: datetime
    source: str


def parse_grib_to_wind_grid(
    path: str | Path,
    source: str = "gfs",
    target_bbox: tuple[float, float, float, float] | None = None,
    target_resolution_deg: float = 0.05,
) -> WindGrid:
    """Read a GRIB2 file and extract 10m wind on a regular lat/lon grid.

    For curvilinear grids (HRRR), regrids onto a regular grid covering
    target_bbox at target_resolution_deg. target_bbox is required for
    curvilinear sources.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    ds = xr.open_dataset(
        path,
        engine="cfgrib",
        filter_by_keys={"typeOfLevel": "heightAboveGround", "level": 10},
        backend_kwargs={"indexpath": ""},
    )
    try:
        if "u10" not in ds.data_vars or "v10" not in ds.data_vars:
            raise ValueError(
                f"Expected u10/v10 in {path}. Got: {list(ds.data_vars)}"
            )

        lats = np.asarray(ds["latitude"].values, dtype=np.float64)
        lons = np.asarray(ds["longitude"].values, dtype=np.float64)
        u = np.asarray(ds["u10"].values, dtype=np.float32)
        v = np.asarray(ds["v10"].values, dtype=np.float32)
        ref = _to_datetime(ds["time"].values)
        valid = _to_datetime(ds["valid_time"].values)
    finally:
        ds.close()

    # Normalize lons to -180..180 BEFORE any grid-specific handling so the
    # bbox filter and regridding both work in the same coordinate convention.
    if lons.max() > 180:
        lons = np.where(lons > 180, lons - 360, lons)

    if lats.ndim == 2 and lons.ndim == 2:
        # Curvilinear (HRRR): regrid to regular lat/lon
        if target_bbox is None:
            raise ValueError(
                f"target_bbox required for curvilinear grid "
                f"(lats {lats.shape}, lons {lons.shape})."
            )
        lats, lons, u, v = _regrid_curvilinear(
            lats, lons, u, v, target_bbox, target_resolution_deg
        )
    elif lats.ndim == 1 and lons.ndim == 1:
        # Regular grid (GFS): re-sort by lon if normalization broke the order
        if not np.all(np.diff(lons) > 0):
            order = np.argsort(lons)
            lons = lons[order]
            u = u[:, order]
            v = v[:, order]
    else:
        raise NotImplementedError(
            f"Unexpected coord shapes: lats {lats.shape}, lons {lons.shape}"
        )

    return WindGrid(
        lats=lats, lons=lons, u=u, v=v,
        reference_time=ref, valid_time=valid, source=source,
    )


# ---------------------------------------------------------------------------
# Curvilinear regridding — precomputed, cached per (grid, bbox, resolution)


@dataclass(frozen=True)
class _CurvilinearRegridder:
    """Precomputed interpolation from one fixed curvilinear grid onto one
    fixed regular target grid.

    ``regrid(values)`` performs the same linear barycentric interpolation
    (inside the source convex hull) + nearest-neighbour fill (outside)
    that ``scipy.interpolate.griddata`` would, but with all geometry —
    triangulation, point location, weights, nearest indices — done once
    at build time. Per call it's a numpy gather + weighted sum.
    """
    tgt_lats: np.ndarray          # 1D float64
    tgt_lons: np.ndarray          # 1D float64
    src_idx: np.ndarray           # flat indices into the source arrays (bbox clip)
    vertices: np.ndarray          # (n_inside, 3) int32 — into clipped source values
    weights: np.ndarray           # (n_inside, 3) float64 — barycentric
    inside: np.ndarray            # (n_tgt,) bool — target pts inside convex hull
    nearest_idx: np.ndarray       # (n_outside,) — into clipped source values
    out_shape: tuple[int, int]    # (len(tgt_lats), len(tgt_lons))

    def regrid(self, values: np.ndarray) -> np.ndarray:
        vals = np.asarray(values, dtype=np.float64).ravel()[self.src_idx]
        out = np.empty(self.inside.shape[0], dtype=np.float64)
        out[self.inside] = np.einsum(
            "nj,nj->n", vals[self.vertices], self.weights,
        )
        out[~self.inside] = vals[self.nearest_idx]
        return out.reshape(self.out_shape).astype(np.float32)


def _build_regridder(
    src_lats: np.ndarray,
    src_lons: np.ndarray,
    bbox: tuple[float, float, float, float],
    resolution: float,
) -> _CurvilinearRegridder:
    from scipy.spatial import Delaunay, cKDTree  # lazy: heavy import

    min_lat, max_lat, min_lon, max_lon = bbox
    buffer = 0.5  # degrees; ensures interp has data on bbox edges

    in_bbox = (
        (src_lats >= min_lat - buffer) & (src_lats <= max_lat + buffer)
        & (src_lons >= min_lon - buffer) & (src_lons <= max_lon + buffer)
    )
    if not in_bbox.any():
        raise ValueError(f"bbox {bbox} doesn't overlap source grid")

    src_idx = np.flatnonzero(in_bbox.ravel())
    pts = np.column_stack([src_lons.ravel()[src_idx], src_lats.ravel()[src_idx]])

    tgt_lats = np.arange(min_lat, max_lat + resolution / 2, resolution)
    tgt_lons = np.arange(min_lon, max_lon + resolution / 2, resolution)
    tgt_lon_mesh, tgt_lat_mesh = np.meshgrid(tgt_lons, tgt_lats)
    tgt_pts = np.column_stack([tgt_lon_mesh.ravel(), tgt_lat_mesh.ravel()])

    # One-time geometry: triangulate, locate, weight. This is the work
    # griddata used to redo on every call.
    tri = Delaunay(pts)
    simplex = tri.find_simplex(tgt_pts)
    inside = simplex >= 0

    trans = tri.transform[simplex[inside]]            # (n_in, 3, 2)
    delta = tgt_pts[inside] - trans[:, 2]             # (n_in, 2)
    bary = np.einsum("nij,nj->ni", trans[:, :2, :], delta)
    weights = np.column_stack([bary, 1.0 - bary.sum(axis=1)])
    vertices = tri.simplices[simplex[inside]].astype(np.int32)

    # Degenerate simplices yield non-finite transforms; route those target
    # points through the nearest-neighbour path so outputs stay finite.
    bad = ~np.isfinite(weights).all(axis=1)
    if bad.any():
        inside_idx = np.flatnonzero(inside)
        inside[inside_idx[bad]] = False
        weights = weights[~bad]
        vertices = vertices[~bad]

    tree = cKDTree(pts)
    _, nearest_idx = tree.query(tgt_pts[~inside])

    # tri and tree go out of scope here — only the small index/weight
    # arrays are retained in the cache.
    return _CurvilinearRegridder(
        tgt_lats=tgt_lats.astype(np.float64),
        tgt_lons=tgt_lons.astype(np.float64),
        src_idx=src_idx,
        vertices=vertices,
        weights=weights,
        inside=inside,
        nearest_idx=nearest_idx,
        out_shape=tgt_lat_mesh.shape,
    )


def _grid_fingerprint(src_lats: np.ndarray, src_lons: np.ndarray) -> tuple:
    """Cheap identity for a curvilinear grid: shape + corner/center coords.

    HRRR's LCC grid is byte-identical across files/cycles; comparing the
    full 1.9M-point arrays per call would defeat the point of caching.
    """
    i, j = src_lats.shape[0] // 2, src_lats.shape[1] // 2
    return (
        src_lats.shape,
        round(float(src_lats[0, 0]), 6), round(float(src_lons[0, 0]), 6),
        round(float(src_lats[-1, -1]), 6), round(float(src_lons[-1, -1]), 6),
        round(float(src_lats[i, j]), 6), round(float(src_lons[i, j]), 6),
    )


# One worker process ingests one (source, region) pair, so this holds at
# most a couple of entries. Module-level so all fhours of a cycle share it.
_REGRIDDER_CACHE: dict[tuple, _CurvilinearRegridder] = {}


def _regrid_curvilinear(
    src_lats: np.ndarray,
    src_lons: np.ndarray,
    src_u: np.ndarray,
    src_v: np.ndarray,
    bbox: tuple[float, float, float, float],
    resolution: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Resample HRRR/LCC wind onto a regular lat/lon grid.

    Geometry (clip → triangulate → locate → weights → nearest-fill
    indices) is built once per (grid, bbox, resolution) and cached;
    per-call work is two numpy weighted gathers.
    """
    key = (_grid_fingerprint(src_lats, src_lons), bbox, float(resolution))
    regridder = _REGRIDDER_CACHE.get(key)
    if regridder is None:
        regridder = _build_regridder(src_lats, src_lons, bbox, resolution)
        _REGRIDDER_CACHE[key] = regridder

    u_grid = regridder.regrid(src_u)
    v_grid = regridder.regrid(src_v)
    return regridder.tgt_lats, regridder.tgt_lons, u_grid, v_grid


def _to_datetime(np_dt) -> datetime:
    """Convert a numpy datetime64 scalar to a tz-aware Python datetime (UTC)."""
    ts = np.datetime64(np_dt, "s").astype("int64")
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)
