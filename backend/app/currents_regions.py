"""Current source registry — NOAA Operational Forecast System (OFS) models.

Kept separate from the wind region registry (``app/regions.py``) because OFS
publishes per-water-body — LMHOFS = Lake Michigan + Huron, CBOFS = Chesapeake,
etc. — and these don't 1:1 map to wind regions. ``conus`` covers all five
Great Lakes plus both coasts; the ``chicago`` venue is a small slice of
LMHOFS. Forcing currents config onto every wind region would distort the
wind contract (e.g., Hawaii has no OFS coverage at all).

Race-time lookup goes ``marks_bbox → sources_covering_marks(...) → 0..N
CurrentSource objects``. The router passes the resulting set to the currents
loader which builds the engine's ``currents=`` sampler.

Grid topology per source (preserved natively — no regridding):

* **FVCOM** (Finite Volume Coastal Ocean Model) — unstructured triangular
  mesh. All five Great Lakes (LMHOFS, LSOFS, LEOFS, LOOFS) plus SFBOFS.
  ``.sample()`` does KDTree nearest-triangle + barycentric interpolation
  to preserve shoreline fidelity near complex embayments.
* **ROMS** (Regional Ocean Modeling System) — curvilinear structured grid.
  CBOFS, DBOFS, TBOFS, GoMOFS, NGOFS2. ``.sample()`` does curvilinear
  bilinear on the model's own 2-D lat/lon arrays.
* **POM** (Princeton Ocean Model) — legacy structured grid. NYOFS only.

NOAA publishes forecast cycles every 6h to:
    https://nomads.ncep.noaa.gov/pub/data/nccf/com/nos/prod/{source}.YYYYMMDD/

File naming: ``nos.{source}.fields.{run_type}{fhour:03d}.{date}.t{cycle:02d}z.nc``
where ``run_type`` is ``f`` (forecast — fhours run forward from cycle start)
or ``n`` (nowcast — fhours run backward to the recent past). The routing
engine cares about forecast fhours; nowcasts are used for the "current
conditions" overlay in the future UI work.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


GridType = Literal["fvcom", "roms", "pom"]
RunType = Literal["f", "n"]


@dataclass(frozen=True)
class CurrentSource:
    """A NOAA OFS model. Each one covers one water body at its native grid.

    ``bbox`` is the model's coverage envelope (min_lat, max_lat, min_lon,
    max_lon). Used for ``contains()`` / ``overlaps_bbox()`` lookups to decide
    which sources to include for a race. Bboxes here are deliberately slightly
    generous — the real model grid mask drops out-of-water points at sample
    time, so a too-tight bbox would miss legitimate coverage near shore.

    ``forecast_horizon_hours`` is the maximum forecast fhour the worker
    should try to ingest. Per-source because the Great Lakes models go to
    f120 while most coastal models stop at f48 or f72.

    ``publish_lag_hours`` is the staleness margin used by ``latest_cycle()``
    in the ingest worker — we don't try to fetch a cycle whose forecast
    files haven't finished publishing yet.
    """

    name: str
    label: str
    bbox: tuple[float, float, float, float]
    grid_type: GridType
    cycle_step_hours: int = 6
    publish_lag_hours: int = 4
    forecast_horizon_hours: int = 48
    nowcast_horizon_hours: int = 6

    def contains(self, lat: float, lon: float) -> bool:
        min_lat, max_lat, min_lon, max_lon = self.bbox
        return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon

    def overlaps_bbox(self, bbox: tuple[float, float, float, float]) -> bool:
        """True if this source's bbox intersects the given bbox at all."""
        b_min_lat, b_max_lat, b_min_lon, b_max_lon = bbox
        s_min_lat, s_max_lat, s_min_lon, s_max_lon = self.bbox
        return not (
            b_max_lat < s_min_lat
            or b_min_lat > s_max_lat
            or b_max_lon < s_min_lon
            or b_min_lon > s_max_lon
        )

    def url_for(self, run_type: RunType, date: str, cycle: int, fhour: int) -> str:
        """Build a NOMADS URL for one NetCDF file.

        Pattern:
            https://.../nos/prod/{source}.YYYYMMDD/
            nos.{source}.fields.{run_type}{fhour:03d}.{date}.t{cycle:02d}z.nc
        """
        if run_type not in ("f", "n"):
            raise ValueError(f"run_type must be 'f' or 'n', got {run_type!r}")
        return (
            "https://nomads.ncep.noaa.gov/pub/data/nccf/com/nos/prod/"
            f"{self.name}.{date}/"
            f"nos.{self.name}.fields.{run_type}{fhour:03d}."
            f"{date}.t{cycle:02d}z.nc"
        )

    def fhour_range(self, run_type: RunType = "f") -> list[int]:
        """fhours to ingest for one cycle, parameterised by run type.

        Forecast (``"f"``) — f000..f{forecast_horizon} inclusive, every
        1h. Includes f000 for consistency with the wind ingest worker
        even though OFS forecast files conventionally start at f001;
        the ingest worker handles a missing f000 the same way it
        handles any 404 (skips and continues).

        Nowcast (``"n"``) — n001..n{nowcast_horizon} inclusive. Nowcast
        files cover the analyzed hours leading UP to the cycle start;
        NOAA convention starts at n001 (one hour before cycle ref time)
        and there is no n000.
        """
        if run_type == "f":
            return list(range(0, self.forecast_horizon_hours + 1))
        if run_type == "n":
            return list(range(1, self.nowcast_horizon_hours + 1))
        raise ValueError(f"run_type must be 'f' or 'n', got {run_type!r}")


# ---------------------------------------------------------------------------
# Source registry
#
# Bbox sources: NOAA OFS model documentation pages. Slightly padded on the
# water side and trimmed of inland fluff so ``contains()`` returns False
# for clearly-non-marine points. If a venue from app/regions.py needs
# currents, the venue's bbox must lie inside one of these.

_SOURCES: tuple[CurrentSource, ...] = (
    # ── Great Lakes (FVCOM) ─────────────────────────────────────────────
    # LMHOFS covers Lake Michigan, Lake Huron, Lake St. Clair, and the
    # connecting channels. East extent reaches -82.0 to cover the Detroit /
    # Lake St. Clair venue.
    CurrentSource(
        name="lmhofs",
        label="Lake Michigan + Huron",
        bbox=(41.5, 46.3, -88.1, -82.0),
        grid_type="fvcom",
        forecast_horizon_hours=120,
    ),
    CurrentSource(
        name="lsofs",
        label="Lake Superior",
        bbox=(46.4, 49.1, -92.2, -84.3),
        grid_type="fvcom",
        forecast_horizon_hours=120,
    ),
    CurrentSource(
        name="leofs",
        label="Lake Erie",
        bbox=(41.3, 43.0, -83.6, -78.7),
        grid_type="fvcom",
        forecast_horizon_hours=120,
    ),
    CurrentSource(
        name="loofs",
        label="Lake Ontario",
        bbox=(43.1, 44.4, -79.9, -75.9),
        grid_type="fvcom",
        forecast_horizon_hours=120,
    ),
    # ── Coastal (FVCOM) ─────────────────────────────────────────────────
    CurrentSource(
        name="sfbofs",
        label="San Francisco Bay",
        bbox=(36.9, 38.3, -123.2, -121.6),
        grid_type="fvcom",
        forecast_horizon_hours=72,
    ),
    # ── Coastal (ROMS) ──────────────────────────────────────────────────
    CurrentSource(
        name="cbofs",
        label="Chesapeake Bay",
        bbox=(36.5, 39.7, -77.4, -75.4),
        grid_type="roms",
        forecast_horizon_hours=48,
    ),
    CurrentSource(
        name="dbofs",
        label="Delaware Bay",
        bbox=(38.3, 40.4, -75.8, -74.4),
        grid_type="roms",
        forecast_horizon_hours=48,
    ),
    CurrentSource(
        name="tbofs",
        label="Tampa Bay",
        bbox=(27.2, 28.1, -83.0, -82.3),
        grid_type="roms",
        forecast_horizon_hours=48,
    ),
    CurrentSource(
        name="gomofs",
        label="Gulf of Maine",
        bbox=(39.0, 45.5, -71.0, -65.0),
        grid_type="roms",
        forecast_horizon_hours=72,
    ),
    CurrentSource(
        name="ngofs2",
        label="Northern Gulf of Mexico",
        bbox=(24.6, 30.6, -98.0, -82.5),
        grid_type="roms",
        forecast_horizon_hours=48,
    ),
    # ── Coastal (POM) ───────────────────────────────────────────────────
    CurrentSource(
        name="nyofs",
        label="NY / NJ Harbor",
        bbox=(40.3, 41.1, -74.5, -73.6),
        grid_type="pom",
        forecast_horizon_hours=48,
    ),
)


CURRENT_SOURCES: dict[str, CurrentSource] = {s.name: s for s in _SOURCES}


# ---------------------------------------------------------------------------
# Lookups


def get(name: str) -> CurrentSource:
    """Lookup; raises KeyError if unknown."""
    return CURRENT_SOURCES[name]


def sources_covering_point(lat: float, lon: float) -> list[CurrentSource]:
    """All OFS sources whose bbox contains (lat, lon). Empty list if none."""
    return [s for s in CURRENT_SOURCES.values() if s.contains(lat, lon)]


def sources_covering_marks(marks: list[dict]) -> list[CurrentSource]:
    """OFS sources whose bbox overlaps the marks' bounding box.

    Used by the routing endpoint: a long race may span the boundary between
    two OFS coverage areas (e.g., a Mac race that crosses out of LMHOFS into
    LOOFS — unlikely on real Mac courses, but the lookup is cheap and the
    engine will only call the source whose bbox actually contains a given
    sample point).
    """
    if not marks:
        return []
    lats = [m["lat"] for m in marks]
    lons = [m["lon"] for m in marks]
    bbox = (min(lats), max(lats), min(lons), max(lons))
    return [s for s in CURRENT_SOURCES.values() if s.overlaps_bbox(bbox)]


def all_source_names() -> list[str]:
    """All source names; used by tests and the rollout runbook."""
    return sorted(CURRENT_SOURCES.keys())


def by_grid_type(grid_type: GridType) -> list[CurrentSource]:
    """All sources matching a given grid family. Tests use this to split
    ingest assertions between FVCOM and ROMS/POM code paths."""
    return [s for s in CURRENT_SOURCES.values() if s.grid_type == grid_type]
