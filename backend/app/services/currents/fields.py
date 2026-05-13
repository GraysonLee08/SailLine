"""Currents samplers — single-snapshot fields + time-bracketing forecast.

Two field classes, one duck-typed interface. The routing engine calls
``field.sample(lat, lon, valid_time) -> (uc_ms, vc_ms) | None`` and doesn't
care whether the underlying grid is unstructured (FVCOM) or curvilinear
structured (ROMS/POM).

* ``FvcomCurrentField`` wraps an ``FvcomMesh`` + ``FvcomSnapshot``. Sampling:
  KDTree of triangle centroids → check K nearest triangles → barycentric
  weights → interpolate u, v. Returns None over land or outside the mesh.

* ``RomsCurrentField`` wraps a ``RomsGrid`` + ``RomsSnapshot``. Sampling:
  KDTree of rho-cell centers → inverse-distance weighting from the K=4
  nearest wet cells. Curvilinear bilinear in (i, j) index space is more
  rigorous but only matters at sub-cell scales we don't care about for
  routing; IDW from neighbors is simpler and falls back gracefully near
  the wet/dry boundary.

* ``CurrentForecast`` is the time wrapper. Mirrors ``WindForecast`` — list
  of field snapshots sorted by valid_time, bisect to bracket, linearly
  interpolate u/v between the bracketing pair. Out-of-window samples
  return None so the engine treats them like a forecast horizon.

* ``CurrentsUnavailable`` is raised by the loader when no OFS source
  covers the race. Currents are optional in the router — the engine
  accepts ``currents=None`` as a no-op — so this exception is
  informational; the route still computes without current-vector
  addition.
"""
from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Protocol, Sequence

import numpy as np

from .netcdf_extract import (
    FvcomMesh,
    FvcomSnapshot,
    RomsGrid,
    RomsSnapshot,
)

log = logging.getLogger(__name__)


# Approximate metres per degree at mid-latitudes. Used only to convert
# the KDTree's degree-space distance into "is this neighbour close enough
# to matter" rejection thresholds; the IDW weights themselves use the
# raw degree distance so the conversion factor cancels out.
_DEG_TO_M = 111_000.0


# ─── Protocols ──────────────────────────────────────────────────────────


class CurrentField(Protocol):
    """Duck-typed interface every field class implements.

    The routing engine consumes anything matching this shape. Defined as
    a Protocol so the engine code stays decoupled from this module.
    """

    source: str
    valid_time: datetime

    def sample(
        self,
        lat: float,
        lon: float,
        valid_time: Optional[datetime] = None,
    ) -> Optional[tuple[float, float]]: ...


class CurrentsUnavailable(Exception):
    """Raised by the loader when no OFS source covers the race window.

    Currents are optional — the router catches this and proceeds with
    ``currents=None`` so the engine path is unaffected. The exception
    carries the attempted source list for logging.
    """

    def __init__(self, attempted_sources: list[str], reason: str = "") -> None:
        self.attempted_sources = attempted_sources
        super().__init__(
            f"no currents available (attempted: {', '.join(attempted_sources) or 'none'})"
            + (f" — {reason}" if reason else "")
        )


# ─── FVCOM field ────────────────────────────────────────────────────────


@dataclass
class FvcomCurrentField:
    """One snapshot of currents on an FVCOM unstructured mesh.

    Sampling cost: O(log N) KDTree query + O(K) barycentric checks where
    K is the number of candidate triangles inspected (default 12). For
    Lake Michigan (~95k triangles), one sample is ~10-30 µs on
    commodity hardware — well below the engine's per-iteration budget.

    The KDTree lives on the underlying ``FvcomMesh`` and is built lazily
    on first sample. Every field sharing the same mesh instance shares
    the index — the loader hands each source's snapshots the same mesh
    so a route compute pays one build per source instead of one per
    fhour.
    """

    mesh: FvcomMesh
    snapshot: FvcomSnapshot

    @property
    def source(self) -> str:
        return self.snapshot.source

    @property
    def valid_time(self) -> datetime:
        return self.snapshot.valid_time

    @property
    def reference_time(self) -> datetime:
        return self.snapshot.reference_time

    def sample(
        self,
        lat: float,
        lon: float,
        valid_time: Optional[datetime] = None,
    ) -> Optional[tuple[float, float]]:
        """Return (u_east_ms, v_north_ms) at (lat, lon), or None.

        ``valid_time`` is accepted for duck-type compatibility with
        WindField/WindForecast but ignored — a single field is one
        snapshot in time. ``CurrentForecast.sample`` is the time-aware
        variant.

        Returns None if the point lies outside the mesh or in a
        masked-out (land / shallow / open boundary) region where the
        three containing-triangle vertices are all NaN.
        """
        del valid_time

        # Fast bbox reject. Mesh bbox is generous to the model's wet
        # extent; out-of-bbox is guaranteed out-of-mesh.
        min_lat, max_lat, min_lon, max_lon = self.mesh.bbox()
        if not (min_lat <= lat <= max_lat and min_lon <= lon <= max_lon):
            return None

        # Lazy index build is cached on the mesh — first sample across
        # ALL fields for this source pays the build; the rest are free.
        tree = self.mesh.kdtree
        centroids = self.mesh.centroids

        # Query K nearest triangle centroids. K=12 is plenty: the FVCOM
        # mesh is roughly Delaunay, so the containing triangle is almost
        # always among the 3-4 nearest centroids; 12 gives generous
        # head-room when the point sits near a sliver triangle.
        k = min(12, centroids.shape[0])
        _, idxs = tree.query(np.array([lat, lon], dtype=np.float64), k=k)
        if k == 1:
            idxs = np.array([idxs])

        tri = self.mesh.triangles
        m_lats = self.mesh.lats
        m_lons = self.mesh.lons
        u_arr = self.snapshot.u
        v_arr = self.snapshot.v

        for tri_idx in idxs:
            n0, n1, n2 = tri[tri_idx]
            bary = _barycentric(
                lat, lon,
                m_lats[n0], m_lons[n0],
                m_lats[n1], m_lons[n1],
                m_lats[n2], m_lons[n2],
            )
            if bary is None:
                continue
            w0, w1, w2 = bary
            # Tiny tolerance to admit points on triangle edges.
            if w0 < -1e-6 or w1 < -1e-6 or w2 < -1e-6:
                continue
            u0, u1, u2 = u_arr[n0], u_arr[n1], u_arr[n2]
            v0, v1, v2 = v_arr[n0], v_arr[n1], v_arr[n2]
            # If any vertex is masked (NaN), bail rather than letting the
            # NaN propagate through the weighted sum. The caller treats
            # None as "no current information here."
            if not (np.isfinite(u0) and np.isfinite(u1) and np.isfinite(u2)
                    and np.isfinite(v0) and np.isfinite(v1) and np.isfinite(v2)):
                return None
            uc = float(w0 * u0 + w1 * u1 + w2 * u2)
            vc = float(w0 * v0 + w1 * v1 + w2 * v2)
            return uc, vc

        # No candidate triangle contained the point — over land, or
        # falls into a hole in the mesh.
        return None


def _barycentric(
    px: float, py: float,
    x0: float, y0: float,
    x1: float, y1: float,
    x2: float, y2: float,
) -> Optional[tuple[float, float, float]]:
    """Barycentric coords of (px, py) in the triangle [(x0,y0), (x1,y1), (x2,y2)].

    Inputs are lat (x) and lon (y) — labels are arbitrary as long as
    they're consistent. Returns None for degenerate (collinear)
    triangles; caller skips them.

    Uses planar geometry (not spherical). Triangle areas in OFS meshes
    are ~1-10 km² on the lakes; planar approximation is good to 5
    decimal places at these scales.
    """
    denom = (y1 - y0) * (x2 - x0) - (y2 - y0) * (x1 - x0)
    if abs(denom) < 1e-15:
        return None
    w1 = ((py - y0) * (x2 - x0) - (y2 - y0) * (px - x0)) / denom
    w2 = ((y1 - y0) * (px - x0) - (py - y0) * (x1 - x0)) / denom
    w0 = 1.0 - w1 - w2
    return w0, w1, w2


# ─── ROMS field ─────────────────────────────────────────────────────────


@dataclass
class RomsCurrentField:
    """One snapshot of currents on a ROMS/POM curvilinear structured grid.

    Sampling: KDTree of rho-cell centres → inverse-distance weighted
    average of the K nearest WET cells. Trades a tiny accuracy loss vs
    full curvilinear bilinear for code simplicity and graceful behaviour
    near the wet/dry boundary (a land cell's NaN is simply skipped
    rather than poisoning a corner-of-the-bilinear weight).

    Index is cached on the underlying ``RomsGrid`` and shared across
    every field that references it — same pattern as
    ``FvcomCurrentField``.
    """

    grid: RomsGrid
    snapshot: RomsSnapshot
    k_neighbors: int = 4

    @property
    def source(self) -> str:
        return self.snapshot.source

    @property
    def valid_time(self) -> datetime:
        return self.snapshot.valid_time

    @property
    def reference_time(self) -> datetime:
        return self.snapshot.reference_time

    def sample(
        self,
        lat: float,
        lon: float,
        valid_time: Optional[datetime] = None,
    ) -> Optional[tuple[float, float]]:
        del valid_time

        # Shared lazy index — first sample across all fields for this
        # source pays the build; the rest are free.
        tree = self.grid.kdtree
        wet_indices = self.grid.wet_indices

        k = min(self.k_neighbors, wet_indices.shape[0])
        if k == 0:
            return None

        dists, idxs = tree.query(
            np.array([lat, lon], dtype=np.float64), k=k,
        )
        if k == 1:
            dists = np.array([dists])
            idxs = np.array([idxs])

        # Reject samples that are too far from any wet cell — keeps the
        # field from extrapolating across a land mass. Threshold is
        # ~10 km in degree-space terms; routing in any OFS-covered
        # water body is well within 10 km of a model cell.
        nearest_m = float(dists[0]) * _DEG_TO_M
        if nearest_m > 10_000.0:
            return None

        u_arr = self.snapshot.u
        v_arr = self.snapshot.v

        # IDW with epsilon to avoid blow-up when the sample sits exactly
        # on a rho centre.
        weights = 1.0 / (dists + 1e-9)
        u_acc = 0.0
        v_acc = 0.0
        w_total = 0.0
        for w, idx in zip(weights, idxs):
            eta, xi = wet_indices[idx]
            u_val = float(u_arr[eta, xi])
            v_val = float(v_arr[eta, xi])
            if not (np.isfinite(u_val) and np.isfinite(v_val)):
                continue
            u_acc += w * u_val
            v_acc += w * v_val
            w_total += w
        if w_total <= 0.0:
            return None
        return u_acc / w_total, v_acc / w_total


# ─── Time-bracketing forecast ───────────────────────────────────────────


@dataclass
class CurrentForecast:
    """Ordered sequence of current-field snapshots covering a race window.

    Snapshots may come from one or multiple OFS sources — they're sorted
    by ``valid_time`` and queried via the same bisect-then-interpolate
    pattern as ``WindForecast``. Multi-source forecasts (e.g., a race
    that straddles LMHOFS and LEOFS) just sample whichever source's
    field contains the (lat, lon); fields outside their own coverage
    return None and the loop falls through to the next snapshot.

    ``quality`` is exposed via the route metadata so the frontend can
    label the route ("currents: lmhofs" vs "currents: lmhofs+leofs").
    """

    snapshots: Sequence[CurrentField]
    quality: str = "unknown"
    _times: list[datetime] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not self.snapshots:
            raise ValueError("CurrentForecast requires at least one snapshot")
        # Defensive sort. _times is the bisect-search axis.
        snaps = sorted(self.snapshots, key=lambda s: s.valid_time)
        self.snapshots = list(snaps)
        self._times = [s.valid_time for s in self.snapshots]

    @property
    def t_min(self) -> datetime:
        return self._times[0]

    @property
    def t_max(self) -> datetime:
        return self._times[-1]

    def covers(self, t: datetime) -> bool:
        return self.t_min <= t <= self.t_max

    def sample(
        self,
        lat: float,
        lon: float,
        valid_time: Optional[datetime] = None,
    ) -> Optional[tuple[float, float]]:
        """Interpolate (u_east_ms, v_north_ms) at (lat, lon, valid_time).

        Returns None outside the forecast time window — engine treats
        this as "past horizon, no current information." Returns None at
        a (lat, lon) outside every snapshot's coverage as well.

        When the requested time falls between two snapshots whose
        coverage areas differ (e.g., LMHOFS ends, LEOFS picks up), the
        sampler prefers a non-None bracket — if the BEFORE snapshot has
        a value at this point and AFTER does not, we return BEFORE's
        value (and vice versa) rather than dropping out.
        """
        if valid_time is None:
            return self.snapshots[0].sample(lat, lon)

        if valid_time < self._times[0] or valid_time > self._times[-1]:
            return None

        i = bisect.bisect_left(self._times, valid_time)
        if i < len(self._times) and self._times[i] == valid_time:
            return self.snapshots[i].sample(lat, lon)

        before = self.snapshots[i - 1]
        after = self.snapshots[i]
        t0 = self._times[i - 1]
        t1 = self._times[i]

        uv0 = before.sample(lat, lon)
        uv1 = after.sample(lat, lon)
        if uv0 is None and uv1 is None:
            return None
        if uv0 is None:
            return uv1
        if uv1 is None:
            return uv0

        span = (t1 - t0).total_seconds()
        if span <= 0:
            return uv0
        a = (valid_time - t0).total_seconds() / span
        u = uv0[0] * (1 - a) + uv1[0] * a
        v = uv0[1] * (1 - a) + uv1[1] * a
        return u, v


# ─── Convenience constructors ───────────────────────────────────────────


def field_for_snapshot(
    topology,
    snapshot,
) -> CurrentField:
    """Wrap a (topology, snapshot) pair in the right field class.

    ``topology`` is the static mesh / grid for the source — FvcomMesh for
    FVCOM sources, RomsGrid for ROMS/POM sources.
    """
    if isinstance(topology, FvcomMesh) and isinstance(snapshot, FvcomSnapshot):
        return FvcomCurrentField(mesh=topology, snapshot=snapshot)
    if isinstance(topology, RomsGrid) and isinstance(snapshot, RomsSnapshot):
        return RomsCurrentField(grid=topology, snapshot=snapshot)
    raise TypeError(
        f"can't build a field from topology={type(topology).__name__} "
        f"+ snapshot={type(snapshot).__name__}"
    )


__all__ = [
    "CurrentField",
    "CurrentForecast",
    "CurrentsUnavailable",
    "FvcomCurrentField",
    "RomsCurrentField",
    "field_for_snapshot",
]
