"""NetCDF -> in-memory native-grid extraction for NOAA OFS files.

Two families of OFS output files, two extraction paths:

* ``extract_fvcom`` reads FVCOM-format NetCDF (Lake Michigan/Huron,
  Superior, Erie, Ontario, SFB). Returns an ``FvcomMesh`` (node coords +
  triangle connectivity — STATIC per source) and an ``FvcomSnapshot``
  (per-fhour u, v at the nodes).
* ``extract_roms`` reads ROMS/POM-format NetCDF (Chesapeake, Delaware,
  Tampa, Gulf of Maine, Northern Gulf of Mexico, NY/NJ Harbor). Returns
  a ``RomsGrid`` (rho-grid lat/lon 2D arrays + wet mask — STATIC per
  source) and a ``RomsSnapshot`` (per-fhour u, v interpolated onto the
  rho grid, rotated to true east/north if the file's ``angle`` variable
  indicates a non-zero grid rotation).

Why split topology from snapshot:
    The mesh / grid is identical across every cycle for a given source.
    Ingesting it once and caching it under ``currents:{source}:mesh``
    keeps the per-fhour blobs small (just u, v, valid_time). Cuts Redis
    footprint roughly 10x compared to embedding the topology in every
    snapshot.

Surface only:
    FVCOM output uses sigma-layer vertical staggering. We take the
    top sigma layer (``siglay=0`` for surface; FVCOM convention is layers
    ordered surface-to-bottom). ROMS uses ``s_rho`` with the topmost
    index = surface in NOAA OFS output conventions. Sailing currents are
    surface currents; deeper layers are out of scope for routing.

Time decoding:
    NOAA OFS uses CF-compliant time units like
    ``"days since 1858-11-17 00:00:00"`` (Modified Julian Date). We
    decode with netCDF4's num2date and convert to a tz-aware UTC
    datetime. One time value per file in OFS output.

The actual byte-range download of these NetCDFs lives in the ingest
worker; this module only knows how to parse a NetCDF on disk.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field as dataclasses_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


# ─── FVCOM ──────────────────────────────────────────────────────────────


@dataclass
class FvcomMesh:
    """Static unstructured triangular mesh for an FVCOM source.

    ``lats``, ``lons`` — node coordinates, shape (n_nodes,), degrees.
    ``triangles`` — connectivity, shape (n_tri, 3), 0-indexed into nodes.
    ``source`` — source name (e.g., "lmhofs") for error messages.

    Not frozen — the lazy spatial index is cached on the instance so
    every field that references this mesh shares one KDTree. Treat the
    public data fields as immutable; the leading-underscore cache
    fields are the only sanctioned mutation point.
    """

    source: str
    lats: np.ndarray
    lons: np.ndarray
    triangles: np.ndarray
    # Lazy spatial index — built on first ``ensure_index()`` call and
    # shared across every ``FvcomCurrentField`` that references this
    # mesh. ``init=False`` keeps construction clean: callers do
    # ``FvcomMesh(source=..., lats=..., lons=..., triangles=...)``.
    _kdtree: Optional[object] = dataclasses_field(default=None, init=False, repr=False, compare=False)
    _centroids: Optional[np.ndarray] = dataclasses_field(default=None, init=False, repr=False, compare=False)

    @property
    def n_nodes(self) -> int:
        return int(self.lats.shape[0])

    @property
    def n_triangles(self) -> int:
        return int(self.triangles.shape[0])

    def bbox(self) -> tuple[float, float, float, float]:
        return (
            float(self.lats.min()),
            float(self.lats.max()),
            float(self.lons.min()),
            float(self.lons.max()),
        )

    def ensure_index(self) -> None:
        """Build the triangle-centroid KDTree if not yet built.

        Idempotent. Field samplers call this on every ``sample`` and
        rely on the second-and-later calls being free.
        """
        if self._kdtree is not None:
            return
        # Lazy scipy import — scipy is in the routing requirements but
        # we don't want simply importing FvcomMesh to drag it in.
        from scipy.spatial import cKDTree  # type: ignore[import-untyped]

        tri = self.triangles
        c_lats = self.lats[tri].mean(axis=1)
        c_lons = self.lons[tri].mean(axis=1)
        centroids = np.column_stack([c_lats, c_lons]).astype(np.float64)
        self._centroids = centroids
        self._kdtree = cKDTree(centroids)

    @property
    def kdtree(self):
        """Lazy spatial index over triangle centroids."""
        self.ensure_index()
        return self._kdtree

    @property
    def centroids(self) -> np.ndarray:
        """(n_tri, 2) array of (lat, lon) centroids, in mesh order."""
        self.ensure_index()
        return self._centroids  # type: ignore[return-value]


@dataclass(frozen=True)
class FvcomSnapshot:
    """One fhour of surface u, v at the FVCOM nodes.

    ``u``, ``v`` are eastward / northward components in m/s, shape
    (n_nodes,) and aligned with the mesh's node ordering.
    """

    source: str
    cycle_iso: str          # "YYYYMMDDTHHMMZ"
    reference_time: datetime
    valid_time: datetime
    fhour: int
    u: np.ndarray
    v: np.ndarray


def extract_fvcom(nc_path: Path, *, source: str, fhour: int) -> tuple[FvcomMesh, FvcomSnapshot]:
    """Parse one NOAA FVCOM forecast file into mesh + snapshot.

    The mesh portion is redundant across every fhour of every cycle for
    a given source — callers may cache it. Returning it here keeps the
    function self-contained for the dry-run / standalone-CLI cases.

    Standard FVCOM variable names used:
        lat, lon            — node coordinates (1-D, n_nodes)
        nv                  — triangle connectivity (3, n_tri), 1-indexed
        u, v                — velocity components (time, siglay, n_nodes)
                              or (time, siglay, nele) in some variants
        latc, lonc          — element centroid coordinates (used as the
                              fall-back node coords when u/v are at
                              elements, not nodes)
        time                — CF-encoded MJD
    """
    # Imported lazily so unit tests can stub the module without netCDF4
    # installed (the package weighs ~10 MB and isn't a hard dep for the
    # rest of the backend).
    import netCDF4  # type: ignore[import-untyped]

    with netCDF4.Dataset(nc_path, "r") as nc:
        # Node coords. FVCOM outputs them as 1-D arrays.
        lats = np.asarray(nc.variables["lat"][:], dtype=np.float32)
        lons = np.asarray(nc.variables["lon"][:], dtype=np.float32)

        # Triangle connectivity. FVCOM stores it as (3, nele) Fortran-style
        # in some builds and (nele, 3) in others — detect and normalise.
        # Convention is 1-indexed; we drop to 0-indexed.
        nv = np.asarray(nc.variables["nv"][:], dtype=np.int64)
        if nv.shape[0] == 3 and nv.shape[1] != 3:
            nv = nv.T
        triangles = nv - 1
        if triangles.min() < 0:
            raise ValueError(
                f"{source}: triangle index < 0 after 0-indexing — input "
                f"connectivity may not be 1-indexed as expected"
            )
        if triangles.max() >= lats.shape[0]:
            raise ValueError(
                f"{source}: triangle index {triangles.max()} exceeds "
                f"node count {lats.shape[0]}"
            )

        # Surface velocities. OFS publishes u, v at sigma layers.
        # siglay=0 is the surface layer per FVCOM convention.
        # Many OFS variants publish u/v at element centroids rather than
        # at nodes; in that case the array's last dim equals n_tri, not
        # n_nodes. Detect and interpolate element -> node via simple area
        # averaging of the three incident triangles' values.
        u_raw = np.asarray(nc.variables["u"][:], dtype=np.float32)
        v_raw = np.asarray(nc.variables["v"][:], dtype=np.float32)
        u_surface = _select_surface_layer(u_raw)
        v_surface = _select_surface_layer(v_raw)

        if u_surface.shape[-1] == triangles.shape[0]:
            log.debug("%s: u,v at element centroids — averaging onto nodes", source)
            u_at_nodes = _element_to_node(u_surface, triangles, n_nodes=lats.shape[0])
            v_at_nodes = _element_to_node(v_surface, triangles, n_nodes=lats.shape[0])
        elif u_surface.shape[-1] == lats.shape[0]:
            u_at_nodes = u_surface
            v_at_nodes = v_surface
        else:
            raise ValueError(
                f"{source}: u trailing dim {u_surface.shape[-1]} matches "
                f"neither node count {lats.shape[0]} nor triangle count "
                f"{triangles.shape[0]}"
            )

        valid_time = _decode_time(nc.variables["time"])

    # Reference time = valid - fhour * 1h. Cycle iso derived from it.
    reference_time = valid_time - _hours(fhour)
    cycle_iso = reference_time.strftime("%Y%m%dT%H%MZ")

    mesh = FvcomMesh(
        source=source,
        lats=lats,
        lons=lons,
        triangles=triangles.astype(np.int32),
    )
    snapshot = FvcomSnapshot(
        source=source,
        cycle_iso=cycle_iso,
        reference_time=reference_time,
        valid_time=valid_time,
        fhour=fhour,
        u=_finite_or_nan(u_at_nodes),
        v=_finite_or_nan(v_at_nodes),
    )
    return mesh, snapshot


def _select_surface_layer(arr: np.ndarray) -> np.ndarray:
    """Reduce a NetCDF u/v variable down to a 1-D surface-layer array.

    Acceptable input shapes:
        (time=1, siglay, n)         -> take time=0, siglay=0
        (siglay, n)                 -> take siglay=0
        (time=1, n)                 -> take time=0  (already surface)
        (n,)                        -> as-is
    """
    if arr.ndim == 3:
        return arr[0, 0, :]
    if arr.ndim == 2:
        # Could be (time, n) with time=1, or (siglay, n). The OFS files
        # always include a sigma dim for 3-D variables, so we assume
        # (siglay, n) when ndim==2 and the first dim is small (<= 30).
        # Otherwise treat the first dim as time.
        if arr.shape[0] <= 30:
            return arr[0, :]
        return arr[0, :]
    if arr.ndim == 1:
        return arr
    raise ValueError(f"unexpected u/v shape: {arr.shape}")


def _element_to_node(values_at_elements: np.ndarray, triangles: np.ndarray, n_nodes: int) -> np.ndarray:
    """Area-weighted average from triangle centroids to nodes.

    For each node, average the values of the (typically 3-6) triangles
    that touch it. Uses simple counts (each touching triangle weighted
    equally) — full area weighting buys ~1% accuracy at significant
    extra cost and matters only at the open boundary, which is masked
    out anyway.
    """
    accum = np.zeros(n_nodes, dtype=np.float64)
    count = np.zeros(n_nodes, dtype=np.int32)
    for tri_idx in range(triangles.shape[0]):
        v_tri = values_at_elements[tri_idx]
        if not np.isfinite(v_tri):
            continue
        for j in range(3):
            node_idx = triangles[tri_idx, j]
            accum[node_idx] += v_tri
            count[node_idx] += 1
    result = np.full(n_nodes, np.nan, dtype=np.float32)
    nonzero = count > 0
    result[nonzero] = (accum[nonzero] / count[nonzero]).astype(np.float32)
    return result


# ─── ROMS / POM ─────────────────────────────────────────────────────────


@dataclass
class RomsGrid:
    """Static curvilinear rho-grid for a ROMS/POM source.

    ``lats``, ``lons`` are 2-D arrays of shape (eta_rho, xi_rho) giving
    the geographic coordinate of each cell center.

    ``mask`` is True where the cell is wet, False where it is land. Used
    by ``sample()`` to return ``None`` over land instead of garbage
    extrapolated values.

    ``angle`` is the grid's local rotation in radians at each cell; used
    to rotate model-frame u, v back to true east/north. Zero for grids
    aligned with lat/lon.

    Not frozen — the lazy spatial index over wet rho cells is cached on
    the instance so every ``RomsCurrentField`` referencing this grid
    shares one KDTree.
    """

    source: str
    lats: np.ndarray
    lons: np.ndarray
    mask: np.ndarray
    angle: np.ndarray
    _kdtree: Optional[object] = dataclasses_field(default=None, init=False, repr=False, compare=False)
    _wet_indices: Optional[np.ndarray] = dataclasses_field(default=None, init=False, repr=False, compare=False)

    def bbox(self) -> tuple[float, float, float, float]:
        wet = self.mask.astype(bool)
        if not wet.any():
            return (float("nan"),) * 4  # type: ignore[return-value]
        return (
            float(self.lats[wet].min()),
            float(self.lats[wet].max()),
            float(self.lons[wet].min()),
            float(self.lons[wet].max()),
        )

    def ensure_index(self) -> None:
        """Build the wet-rho-cell KDTree if not yet built. Idempotent."""
        if self._kdtree is not None:
            return
        from scipy.spatial import cKDTree  # type: ignore[import-untyped]

        wet = self.mask.astype(bool)
        if not wet.any():
            raise ValueError(f"{self.source}: ROMS mask is entirely dry")
        eta_idx, xi_idx = np.where(wet)
        self._wet_indices = np.column_stack([eta_idx, xi_idx]).astype(np.int32)
        coords = np.column_stack([self.lats[wet], self.lons[wet]]).astype(np.float64)
        self._kdtree = cKDTree(coords)

    @property
    def kdtree(self):
        """Lazy spatial index over wet rho cells."""
        self.ensure_index()
        return self._kdtree

    @property
    def wet_indices(self) -> np.ndarray:
        """(n_wet, 2) array of (eta, xi) indices, aligned with kdtree rows."""
        self.ensure_index()
        return self._wet_indices  # type: ignore[return-value]


@dataclass(frozen=True)
class RomsSnapshot:
    """One fhour of surface u, v on the rho grid.

    ``u`` and ``v`` are eastward / northward components in m/s, shape
    (eta_rho, xi_rho), already de-staggered from the C-grid and rotated
    to true east/north using the grid's angle.
    """

    source: str
    cycle_iso: str
    reference_time: datetime
    valid_time: datetime
    fhour: int
    u: np.ndarray
    v: np.ndarray


def extract_roms(nc_path: Path, *, source: str, fhour: int) -> tuple[RomsGrid, RomsSnapshot]:
    """Parse one NOAA ROMS/POM forecast file into rho-grid + snapshot.

    Reads from the C-grid (u on u-points, v on v-points), de-staggers to
    rho points by simple averaging, and rotates to true east/north using
    the grid's ``angle`` variable when present.

    Variables consulted:
        lat_rho, lon_rho            — rho-grid coordinates (2-D)
        mask_rho                    — wet/dry mask (2-D, 0/1)
        angle                       — grid rotation in radians (2-D)
        u                           — (time, s_rho, eta_rho, xi_u)
        v                           — (time, s_rho, eta_v, xi_rho)
        time                        — CF-encoded
    """
    import netCDF4  # type: ignore[import-untyped]

    with netCDF4.Dataset(nc_path, "r") as nc:
        lats = np.asarray(nc.variables["lat_rho"][:], dtype=np.float32)
        lons = np.asarray(nc.variables["lon_rho"][:], dtype=np.float32)
        mask = np.asarray(nc.variables["mask_rho"][:], dtype=bool)
        angle = (
            np.asarray(nc.variables["angle"][:], dtype=np.float32)
            if "angle" in nc.variables
            else np.zeros_like(lats, dtype=np.float32)
        )

        u_raw = np.asarray(nc.variables["u"][:], dtype=np.float32)
        v_raw = np.asarray(nc.variables["v"][:], dtype=np.float32)

        u_surface_ugrid = _select_surface_layer_4d(u_raw)
        v_surface_vgrid = _select_surface_layer_4d(v_raw)

        u_rho = _u_to_rho(u_surface_ugrid, eta_rho=lats.shape[0], xi_rho=lats.shape[1])
        v_rho = _v_to_rho(v_surface_vgrid, eta_rho=lats.shape[0], xi_rho=lats.shape[1])

        # Rotate from model frame to geographic (true east/north).
        cos_a = np.cos(angle)
        sin_a = np.sin(angle)
        u_east = u_rho * cos_a - v_rho * sin_a
        v_north = u_rho * sin_a + v_rho * cos_a

        # Apply land mask — NaN on land so the field sampler can return
        # None instead of garbage.
        u_east = np.where(mask, u_east, np.nan).astype(np.float32)
        v_north = np.where(mask, v_north, np.nan).astype(np.float32)

        valid_time = _decode_time(nc.variables["time"])

    reference_time = valid_time - _hours(fhour)
    cycle_iso = reference_time.strftime("%Y%m%dT%H%MZ")

    grid = RomsGrid(
        source=source,
        lats=lats,
        lons=lons,
        mask=mask,
        angle=angle,
    )
    snapshot = RomsSnapshot(
        source=source,
        cycle_iso=cycle_iso,
        reference_time=reference_time,
        valid_time=valid_time,
        fhour=fhour,
        u=u_east,
        v=v_north,
    )
    return grid, snapshot


def _select_surface_layer_4d(arr: np.ndarray) -> np.ndarray:
    """Reduce a ROMS u/v variable to a 2-D surface-layer array.

    Accepted shapes:
        (time=1, s_rho, eta, xi)    -> [0, -1]    (top s_rho = surface)
        (s_rho, eta, xi)            -> [-1]
        (time=1, eta, xi)           -> [0]        (already surface)
        (eta, xi)                   -> as-is
    """
    if arr.ndim == 4:
        return arr[0, -1, :, :]
    if arr.ndim == 3:
        # Could be (time, eta, xi) or (s_rho, eta, xi). Tell apart by
        # the size of the first axis: typically 1 for time, >1 for s_rho.
        if arr.shape[0] == 1:
            return arr[0, :, :]
        return arr[-1, :, :]
    if arr.ndim == 2:
        return arr
    raise ValueError(f"unexpected ROMS u/v shape: {arr.shape}")


def _u_to_rho(u_ugrid: np.ndarray, *, eta_rho: int, xi_rho: int) -> np.ndarray:
    """De-stagger a C-grid u variable (eta_rho, xi_u) onto the rho grid.

    Linear interpolation in xi: u_rho[:, i] = 0.5 * (u[:, i-1] + u[:, i]).
    The two boundary columns copy the nearest u-face value — a small
    error confined to a half-cell at the lateral boundaries, far from
    racing waters.
    """
    if u_ugrid.shape != (eta_rho, xi_rho - 1):
        raise ValueError(
            f"u shape {u_ugrid.shape} incompatible with rho shape "
            f"({eta_rho}, {xi_rho})"
        )
    out = np.empty((eta_rho, xi_rho), dtype=np.float32)
    out[:, 1:-1] = 0.5 * (u_ugrid[:, :-1] + u_ugrid[:, 1:])
    out[:, 0] = u_ugrid[:, 0]
    out[:, -1] = u_ugrid[:, -1]
    return out


def _v_to_rho(v_vgrid: np.ndarray, *, eta_rho: int, xi_rho: int) -> np.ndarray:
    """De-stagger a C-grid v variable (eta_v, xi_rho) onto the rho grid."""
    if v_vgrid.shape != (eta_rho - 1, xi_rho):
        raise ValueError(
            f"v shape {v_vgrid.shape} incompatible with rho shape "
            f"({eta_rho}, {xi_rho})"
        )
    out = np.empty((eta_rho, xi_rho), dtype=np.float32)
    out[1:-1, :] = 0.5 * (v_vgrid[:-1, :] + v_vgrid[1:, :])
    out[0, :] = v_vgrid[0, :]
    out[-1, :] = v_vgrid[-1, :]
    return out


# ─── Shared helpers ─────────────────────────────────────────────────────


def _decode_time(time_var) -> datetime:
    """Decode the NetCDF time variable to a tz-aware UTC datetime.

    OFS NetCDFs carry one time value per file. Some publishers report it
    as a 0-d scalar, some as a 1-element vector; we accept either.
    """
    import netCDF4  # type: ignore[import-untyped]

    units = time_var.units
    cal = getattr(time_var, "calendar", "standard")
    raw = time_var[:]
    if hasattr(raw, "ndim") and raw.ndim > 0:
        raw = raw[0]
    decoded = netCDF4.num2date(
        raw, units=units, calendar=cal,
        only_use_cftime_datetimes=False,
        only_use_python_datetimes=True,
    )
    if isinstance(decoded, datetime):
        # netCDF4 returns naive UTC; tag it.
        return decoded.replace(tzinfo=timezone.utc)
    # cftime objects support .year/.month/etc and can be converted.
    return datetime(
        decoded.year, decoded.month, decoded.day,
        decoded.hour, decoded.minute, int(decoded.second),
        tzinfo=timezone.utc,
    )


def _hours(n: int):
    from datetime import timedelta
    return timedelta(hours=int(n))


def _finite_or_nan(arr: np.ndarray) -> np.ndarray:
    """Coerce masked-array fills / huge sentinels to NaN.

    NOAA OFS often returns ``_FillValue`` of 1e37 or similar over land /
    out-of-domain cells; we want NaN so sample-time logic can short-circuit.
    """
    out = np.asarray(arr, dtype=np.float32)
    bad = ~np.isfinite(out) | (np.abs(out) > 1e6)
    out = np.where(bad, np.nan, out).astype(np.float32)
    return out


def extract(
    nc_path: Path,
    *,
    source: str,
    grid_type: str,
    fhour: int,
):
    """Convenience dispatcher used by the ingest worker.

    Returns ``(mesh_or_grid, snapshot)``. Callers that know the grid type
    statically should call ``extract_fvcom`` / ``extract_roms`` directly.
    """
    if grid_type == "fvcom":
        return extract_fvcom(nc_path, source=source, fhour=fhour)
    if grid_type in ("roms", "pom"):
        return extract_roms(nc_path, source=source, fhour=fhour)
    raise ValueError(f"unknown grid_type: {grid_type!r}")
