# backend/app/services/routing/wind_forecast.py
"""Time-aware wind sampling across a sequence of forecast snapshots.

A WindForecast is a list of WindField snapshots ordered by valid_time.
sample(lat, lon, t) finds the bracketing pair via bisect and linearly
interpolates u/v. Edges fall back to the nearest snapshot. Times outside
the forecast horizon return None - the engine treats that as 'no wind
information here, don't expand from this node.'

Persist-last-frame (v12): with ``persist_beyond_horizon=True``, times
PAST t_max return the last snapshot's wind instead of None, so the
engine can keep routing to the finish when the course outruns the
forecast window. The persisted tail is a steady-state assumption, not
a forecast - callers flag it in route metadata (``horizon_exceeded``)
and the recompute worker replaces it with real data on the next
ingest. Times BEFORE t_min still return None regardless (the loader
always brackets race_start, so pre-t_min samples indicate a bug, not
a horizon problem).

Mixed-source sequences (HRRR + GFS) are concatenated and ordered by
valid_time. We deliberately do NOT cross-blend the boundary: each
sample falls inside one source's interval. The lat/lon grids may differ
between sources; bilinear sampling happens within whichever WindField
the time bracket lands in.

The engine duck-types: it just calls wind.sample(lat, lon, valid_time).
WindField.sample accepts an optional valid_time and ignores it; tests
that pre-date the forecast refactor keep working.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from app.services.routing.isochrone import WindField


def _parse_iso(s: str) -> datetime:
    # Python <3.11 doesn't accept "Z" suffix; normalise.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


@dataclass
class WindForecast:
    """Ordered sequence of WindField snapshots covering a race window.

    Attributes:
        snapshots: WindField list ordered by valid_time ascending.
        quality:   "hrrr", "gfs", or "hybrid" - exposed in route metadata
                   so the frontend can label preliminary vs final routes.
        persist_beyond_horizon:
                   when True, sample() past t_max returns the LAST
                   snapshot's wind instead of None (persist-last-frame).
                   Default False preserves the historical hard-stop.
    """
    snapshots: list[WindField]
    quality: str = "hybrid"
    persist_beyond_horizon: bool = False
    _times: list[datetime] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not self.snapshots:
            raise ValueError("WindForecast requires at least one snapshot")
        # Sort defensively. _times is the bisect-search axis.
        self.snapshots = sorted(
            self.snapshots,
            key=lambda s: _parse_iso(s.valid_time) if s.valid_time else datetime.min,
        )
        self._times = [
            _parse_iso(s.valid_time) for s in self.snapshots if s.valid_time
        ]
        if len(self._times) != len(self.snapshots):
            raise ValueError("every WindField in a WindForecast must have valid_time set")

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
        """Linearly interpolate u/v at (lat, lon, valid_time).

        If valid_time is before t_min, returns None. If valid_time is
        past t_max: returns None (engine stops expanding — the
        historical behaviour) unless ``persist_beyond_horizon`` is set,
        in which case the last snapshot's wind is returned so routing
        can continue on a steady-state assumption.

        If valid_time is None, samples the first snapshot. This makes
        WindForecast usable in legacy callers that don't pass time.
        """
        if valid_time is None:
            return self.snapshots[0].sample(lat, lon)

        if valid_time < self._times[0]:
            return None
        if valid_time > self._times[-1]:
            if self.persist_beyond_horizon:
                return self.snapshots[-1].sample(lat, lon)
            return None

        # Find bracketing pair.
        i = bisect.bisect_left(self._times, valid_time)
        if i < len(self._times) and self._times[i] == valid_time:
            return self.snapshots[i].sample(lat, lon)
        # i is the insertion index -> snapshot i-1 is before, i is after.
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

        # Linear interpolation in u/v space. Direction near calm becomes
        # ill-defined (e.g. (0.1, 0.1) and (-0.1, -0.1) average to zero
        # wind) but the boat barely moves either way, so it doesn't
        # affect routing in practice.
        span = (t1 - t0).total_seconds()
        if span <= 0:
            return uv0
        a = (valid_time - t0).total_seconds() / span
        u = uv0[0] * (1 - a) + uv1[0] * a
        v = uv0[1] * (1 - a) + uv1[1] * a
        return (u, v)
