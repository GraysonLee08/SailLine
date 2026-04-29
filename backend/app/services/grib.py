"""Parse NOAA GRIB2 files into 10m wind grids on regular lat/lon grids.

GFS arrives on a regular grid (1D lat/lon coords) and is used as-is.
HRRR arrives on a Lambert Conformal Conic grid (2D coords) and is
regridded here so all downstream consumers see the same shape.
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


def _regrid_curvilinear(
    src_lats: np.ndarray,
    src_lons: np.ndarray,
    src_u: np.ndarray,
    src_v: np.ndarray,
    bbox: tuple[float, float, float, float],
    resolution: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Resample HRRR/LCC wind onto a regular lat/lon grid.

    1. Clip source cells to bbox + buffer (keeps interpolation cheap)
    2. Linear interp onto the target grid
    3. Fill any NaN (outside convex hull) with nearest-neighbor
    """
    from scipy.interpolate import griddata  # lazy: heavy import

    min_lat, max_lat, min_lon, max_lon = bbox
    buffer = 0.5  # degrees; ensures interp has data on bbox edges

    in_bbox = (
        (src_lats >= min_lat - buffer) & (src_lats <= max_lat + buffer)
        & (src_lons >= min_lon - buffer) & (src_lons <= max_lon + buffer)
    )
    if not in_bbox.any():
        raise ValueError(f"bbox {bbox} doesn't overlap source grid")

    pts = np.column_stack([src_lons[in_bbox], src_lats[in_bbox]])
    u_flat = src_u[in_bbox]
    v_flat = src_v[in_bbox]

    tgt_lats = np.arange(min_lat, max_lat + resolution / 2, resolution)
    tgt_lons = np.arange(min_lon, max_lon + resolution / 2, resolution)
    tgt_lon_mesh, tgt_lat_mesh = np.meshgrid(tgt_lons, tgt_lats)
    tgt_pts = np.column_stack([tgt_lon_mesh.ravel(), tgt_lat_mesh.ravel()])

    u_grid = griddata(pts, u_flat, tgt_pts, method="linear")
    v_grid = griddata(pts, v_flat, tgt_pts, method="linear")

    # Fill any NaNs at convex-hull edges with nearest-neighbor
    if np.isnan(u_grid).any():
        u_grid = np.where(
            np.isnan(u_grid),
            griddata(pts, u_flat, tgt_pts, method="nearest"),
            u_grid,
        )
    if np.isnan(v_grid).any():
        v_grid = np.where(
            np.isnan(v_grid),
            griddata(pts, v_flat, tgt_pts, method="nearest"),
            v_grid,
        )

    u_grid = u_grid.reshape(tgt_lat_mesh.shape).astype(np.float32)
    v_grid = v_grid.reshape(tgt_lat_mesh.shape).astype(np.float32)
    return tgt_lats.astype(np.float64), tgt_lons.astype(np.float64), u_grid, v_grid


def _to_datetime(np_dt) -> datetime:
    """Convert a numpy datetime64 scalar to a tz-aware Python datetime (UTC)."""
    ts = np.datetime64(np_dt, "s").astype("int64")
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)