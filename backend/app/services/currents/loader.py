"""Load a CurrentForecast for a race from cached OFS cycles.

Mirrors the role of ``services/weather/forecast_loader.py`` for currents:
given the race window and the set of OFS sources whose bbox overlaps the
marks, build a time-ordered ``CurrentForecast`` covering as much of
[race_start, race_end] as the ingested cycles can provide.

The router calls this once per route compute; the resulting
``CurrentForecast`` becomes the engine's ``currents=`` argument.

Resolution rules:

1. Each OFS source is considered independently — fetch its newest cycle
   from ``currents:{source}:cycles`` and inspect both the nowcast and
   forecast manifests for that cycle.
2. Pick fhours whose valid_time intersects [race_start, race_end], plus
   one bracketing fhour on each side so the time-interpolation in
   ``CurrentForecast.sample`` doesn't drop out at the edges.
3. Prefer nowcast samples in the analyzed past, forecast samples for
   future times. They have the same source-grid topology and identical
   valid_time encoding so the engine sees a uniform sequence.
4. Snapshots from multiple sources are concatenated in valid_time order.
   At a given (lat, lon, t), each source's field returns None outside
   its own coverage; ``CurrentForecast`` already prefers the non-None
   bracket so multi-source forecasts degrade gracefully.

Failure modes:

* ``CurrentsUnavailable`` — no source for the marks bbox has any
  ingested cycle yet. The router catches this and proceeds with
  ``currents=None``; the engine treats absent currents as a no-op.
* ``RuntimeError`` — manifest references a snapshot blob that's
  missing from Redis. An operational bug, surfaced as a 503.
"""
from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

from app import redis_client
from app.currents_regions import CurrentSource

from .fields import (
    CurrentField,
    CurrentForecast,
    CurrentsUnavailable,
    FvcomCurrentField,
    RomsCurrentField,
)
from .netcdf_extract import (
    FvcomMesh,
    FvcomSnapshot,
    RomsGrid,
    RomsSnapshot,
)

log = logging.getLogger(__name__)

DEFAULT_RACE_DURATION_HOURS = 6.0


# ─── Public entrypoint ──────────────────────────────────────────────────


async def load_currents_for_race(
    sources: list[CurrentSource],
    race_start: datetime,
    duration_hours: float = DEFAULT_RACE_DURATION_HOURS,
) -> CurrentForecast:
    """Build a CurrentForecast covering [race_start, race_end].

    ``sources`` is the OFS source set returned by
    ``app.currents_regions.sources_covering_marks(...)``. Empty list is
    a valid input (race is in a region with no OFS coverage) and raises
    ``CurrentsUnavailable`` so the router can fall back to no-currents.
    """
    if not sources:
        raise CurrentsUnavailable([], "no OFS source covers the marks bbox")

    if race_start.tzinfo is None:
        race_start = race_start.replace(tzinfo=timezone.utc)
    race_end = race_start + timedelta(hours=duration_hours)

    attempted = [s.name for s in sources]
    fields: list[CurrentField] = []
    quality_parts: list[str] = []

    for source in sources:
        cycle_iso = await _newest_cycle(source.name)
        if cycle_iso is None:
            log.warning(
                "currents source %s has no ingested cycle yet, skipping",
                source.name,
            )
            continue

        topology = await _load_topology(source)
        if topology is None:
            log.warning(
                "currents source %s cycle=%s — topology missing, skipping",
                source.name, cycle_iso,
            )
            continue

        # Both run types contribute to one merged snapshot list per
        # source. A 'missing' manifest is non-fatal — the matching ingest
        # job may not have run yet.
        manifests = await _load_manifests(source.name, cycle_iso)
        if not manifests:
            log.warning(
                "currents source %s cycle=%s — no manifests found, skipping",
                source.name, cycle_iso,
            )
            continue

        picks = _pick_bracketing(manifests, race_start, race_end)
        if not picks:
            log.info(
                "currents source %s cycle=%s — no fhours intersect race window",
                source.name, cycle_iso,
            )
            continue

        loaded = 0
        for run_type, fhour, valid_time in picks:
            snapshot = await _load_snapshot(
                source, cycle_iso, run_type, fhour, topology,
            )
            if snapshot is None:
                continue
            fields.append(_build_field(topology, snapshot))
            loaded += 1

        if loaded:
            quality_parts.append(source.name)
            log.info(
                "currents %s cycle=%s — loaded %d fhours",
                source.name, cycle_iso, loaded,
            )

    if not fields:
        raise CurrentsUnavailable(
            attempted,
            f"no snapshots in [{race_start.isoformat()}, {race_end.isoformat()}]",
        )

    quality = "+".join(quality_parts)
    return CurrentForecast(snapshots=fields, quality=quality)


# ─── Redis I/O ──────────────────────────────────────────────────────────


async def _newest_cycle(source_name: str) -> Optional[str]:
    """Return the most recent cycle iso ingested for this source, or None."""
    redis = redis_client.get_client()
    key = f"currents:{source_name}:cycles"
    raw = await redis.zrevrange(key, 0, 0)
    if not raw:
        return None
    val = raw[0]
    return val.decode() if isinstance(val, bytes) else val


async def _load_topology(source: CurrentSource):
    """Read the static mesh / grid blob, return the appropriate dataclass."""
    redis = redis_client.get_client()
    blob = await redis.get(f"currents:{source.name}:topology")
    if blob is None:
        return None
    payload = json.loads(gzip.decompress(blob))
    return _deserialize_topology(payload)


async def _load_manifests(
    source_name: str, cycle_iso: str,
) -> list[tuple[str, int, datetime]]:
    """Merge nowcast + forecast manifests into a (run_type, fhour, vt) list.

    A missing manifest (e.g., nowcast hasn't run yet for this cycle)
    just contributes zero entries.
    """
    redis = redis_client.get_client()
    out: list[tuple[str, int, datetime]] = []

    for run_type in ("n", "f"):
        key = f"currents:{source_name}:{cycle_iso}:{run_type}_manifest"
        blob = await redis.get(key)
        if blob is None:
            continue
        manifest = json.loads(blob)
        fhours = manifest.get("fhours", [])
        valid_times = manifest.get("valid_times", [])
        for fh, vt in zip(fhours, valid_times):
            out.append((run_type, int(fh), _parse_iso(vt)))

    return out


async def _load_snapshot(
    source: CurrentSource,
    cycle_iso: str,
    run_type: str,
    fhour: int,
    topology,
):
    """Read one fhour blob, return the appropriate snapshot dataclass."""
    redis = redis_client.get_client()
    key = f"currents:{source.name}:{cycle_iso}:{run_type}{fhour:03d}"
    blob = await redis.get(key)
    if blob is None:
        log.warning(
            "currents snapshot %s missing from Redis (manifest referenced it)",
            key,
        )
        return None
    payload = json.loads(gzip.decompress(blob))
    return _deserialize_snapshot(payload, topology=topology)


# ─── Bracketing ─────────────────────────────────────────────────────────


def _pick_bracketing(
    manifest_entries: list[tuple[str, int, datetime]],
    t_start: datetime,
    t_end: datetime,
) -> list[tuple[str, int, datetime]]:
    """Pick the smallest set of fhours that brackets [t_start, t_end].

    Same shape as the wind loader's helper but operates on a merged
    nowcast+forecast list. The list is sorted by valid_time before
    selection so the bracketing logic is run-type-agnostic.

    Returns entries in valid_time order. Includes:

    * every entry whose valid_time falls inside the window
    * the latest entry before the window (if any) — left bracket
    * the earliest entry after the window (if any) — right bracket

    When nowcast and forecast both happen to publish the same valid_time
    (typically forecast f000 vs nowcast's most-recent — they coincide at
    the cycle reference time), the forecast entry is preferred. Pure
    convention: the cycle's f000 is what later cycles will reproduce
    most consistently, making cache behaviour more stable.
    """
    if not manifest_entries:
        return []

    # Sort by (valid_time, run_type) where 'n' < 'f' so the dedup
    # preference below picks 'f' for identical valid_times.
    sorted_entries = sorted(manifest_entries, key=lambda e: (e[2], 0 if e[0] == "n" else 1))

    # Dedup by valid_time — last wins, so forecast trumps nowcast at
    # the cycle reference time.
    by_vt: dict[datetime, tuple[str, int, datetime]] = {}
    for entry in sorted_entries:
        by_vt[entry[2]] = entry
    deduped = sorted(by_vt.values(), key=lambda e: e[2])

    in_window = [e for e in deduped if t_start <= e[2] <= t_end]
    selected: dict[datetime, tuple[str, int, datetime]] = {e[2]: e for e in in_window}

    before = [e for e in deduped if e[2] < t_start]
    if before:
        bracket = max(before, key=lambda e: e[2])
        selected.setdefault(bracket[2], bracket)

    after = [e for e in deduped if e[2] > t_end]
    if after:
        bracket = min(after, key=lambda e: e[2])
        selected.setdefault(bracket[2], bracket)

    return sorted(selected.values(), key=lambda e: e[2])


# ─── Deserialisation ────────────────────────────────────────────────────


def _deserialize_topology(payload: dict):
    """Inverse of ``workers.currents_ingest._serialize_topology``."""
    kind = payload["kind"]
    source = payload["source"]
    if kind == "fvcom":
        return FvcomMesh(
            source=source,
            lats=np.asarray(payload["lats"], dtype=np.float32),
            lons=np.asarray(payload["lons"], dtype=np.float32),
            triangles=np.asarray(payload["triangles"], dtype=np.int32),
        )
    if kind == "roms":
        return RomsGrid(
            source=source,
            lats=np.asarray(payload["lats"], dtype=np.float32),
            lons=np.asarray(payload["lons"], dtype=np.float32),
            mask=np.asarray(payload["mask"], dtype=bool),
            angle=np.asarray(payload["angle"], dtype=np.float32),
        )
    raise ValueError(f"unknown topology kind: {kind!r}")


def _deserialize_snapshot(payload: dict, *, topology):
    """Inverse of ``workers.currents_ingest._serialize_snapshot``.

    ``topology`` is passed in so the snapshot can be constructed against
    the right grid shape; FVCOM expects 1-D u, v aligned with the mesh
    nodes, ROMS expects 2-D u, v on the rho grid.
    """
    kind = payload["kind"]
    source = payload["source"]
    cycle_iso = payload["cycle"]
    fhour = int(payload["fhour"])
    reference_time = _parse_iso(payload["reference_time"])
    valid_time = _parse_iso(payload["valid_time"])

    u = _from_finite_list(payload["u"])
    v = _from_finite_list(payload["v"])

    if kind == "fvcom":
        if not isinstance(topology, FvcomMesh):
            raise TypeError(f"topology/snapshot kind mismatch for {source}")
        return FvcomSnapshot(
            source=source,
            cycle_iso=cycle_iso,
            reference_time=reference_time,
            valid_time=valid_time,
            fhour=fhour,
            u=u.astype(np.float32),
            v=v.astype(np.float32),
        )
    if kind == "roms":
        if not isinstance(topology, RomsGrid):
            raise TypeError(f"topology/snapshot kind mismatch for {source}")
        shape = tuple(payload["shape"])
        return RomsSnapshot(
            source=source,
            cycle_iso=cycle_iso,
            reference_time=reference_time,
            valid_time=valid_time,
            fhour=fhour,
            u=u.reshape(shape).astype(np.float32),
            v=v.reshape(shape).astype(np.float32),
        )
    raise ValueError(f"unknown snapshot kind: {kind!r}")


def _build_field(topology, snapshot) -> CurrentField:
    """Pair a topology + snapshot into the right field class.

    Inlined here rather than calling ``fields.field_for_snapshot`` so
    the loader keeps a flat import graph and any deserialisation drift
    surfaces here at parse time, not deep in the engine sample loop.
    """
    if isinstance(topology, FvcomMesh) and isinstance(snapshot, FvcomSnapshot):
        return FvcomCurrentField(mesh=topology, snapshot=snapshot)
    if isinstance(topology, RomsGrid) and isinstance(snapshot, RomsSnapshot):
        return RomsCurrentField(grid=topology, snapshot=snapshot)
    raise TypeError(
        f"can't build field from topology={type(topology).__name__} "
        f"+ snapshot={type(snapshot).__name__}"
    )


# ─── Helpers ────────────────────────────────────────────────────────────


def _parse_iso(s: str) -> datetime:
    """Decode an ISO-8601 string back to a tz-aware UTC datetime.

    Same Python-3.10 'Z' workaround as the wind loader.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _from_finite_list(nested) -> np.ndarray:
    """Inverse of ``workers.currents_ingest._to_finite_list``.

    JSON ``None`` (masked / no-data) round-trips back to NaN so the
    field samplers can short-circuit at masked points.
    """
    arr = np.asarray(nested, dtype=object)
    out = np.where(arr == None, np.nan, arr)  # noqa: E711 — element-wise None check
    return out.astype(np.float32)


__all__ = [
    "DEFAULT_RACE_DURATION_HOURS",
    "load_currents_for_race",
]
