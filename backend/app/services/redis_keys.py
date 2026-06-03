# backend/app/services/redis_keys.py
"""Single source of truth for Redis key shapes used across the app.

Every producer (ingest workers) and every consumer (routers, services,
background workers) imports from this module. Before this lived as a
module, the same key was f-stringed in 5+ files; tweaking the cycle
encoding or run-type position in the producer would silently break the
consumer because no test pinned the contract.

Key conventions
---------------
* All keys are written to db=0 of the project's Memorystore instance.
* Cycle ids are the compact ISO form ``YYYYMMDDTHHMMZ`` (UTC, no
  separators) — that is what ingest workers ``strftime`` and what
  consumers ``strptime`` with ``%Y%m%dT%H%MZ``.
* ``fhour`` is always written zero-padded width 3 (``f018``, ``n006``)
  even for the wind worker that ingests F00-F18 only. Consistency
  beats compactness.
* Run types in the currents space are the single chars ``"n"``
  (nowcast) and ``"f"`` (forecast). The Run-type letter precedes the
  fhour in snapshot keys (``n006``); manifest keys use the ``_manifest``
  suffix (``n_manifest``).

What lives here vs. not
-----------------------
* Yes: key strings, channel names, route-cache TTL constants, the
  notification TTL.
* No: per-source weather/currents TTLs — those vary by source (HRRR's
  cycle TTL differs from GFS's because the publishing cadence does),
  and they live next to the ``Source`` dataclass in the ingest worker.
* No: ``ENGINE_VERSION`` — that lives next to the routing pipeline
  (see ``app/services/routing/pipeline.py``) so a knob added to
  ``RouteRequest`` and the version bump land in the same file.

Pin contract via fingerprint tests in ``tests/test_redis_keys.py``:
each builder is invoked with fixed inputs and the expected string is
asserted verbatim. Change a key shape ⇒ the fingerprint test fails
loudly *at unit-test time* rather than as a silent cache miss in prod.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional, Union
from uuid import UUID


# ---------------------------------------------------------------------------
# TTL constants (only the ones that aren't per-source)


# 1 hour. A cached route is invalidated by ENGINE_VERSION bumps, by a new
# weather/currents cycle landing (cycle id is in the key), and by
# user-tunable derating changes. The hour TTL is the floor — covers the
# pathological "user submits same race over and over" pattern without
# letting yesterday's forecast bleed into today's compute.
ROUTE_CACHE_TTL_S: int = 3600


# 7 days. Keeps a week of better-route alerts so the user can review
# missed notifications. Also used for the baseline ``route:last_best``
# key — a race that goes quiet for a week should restart its baseline
# rather than triggering a stale apples-vs-oranges alert.
ROUTE_NOTIFICATION_TTL_S: int = 7 * 24 * 3600


# 7 days. Mirrors notification TTL. The worker reads this on every
# recompute pass; it's a cache, not a source of truth — falling back to
# defaults when missing is the documented behaviour.
ROUTE_LAST_REQUEST_TTL_S: int = 7 * 24 * 3600


# ---------------------------------------------------------------------------
# Weather (HRRR / GFS / future ECMWF wind ingest)


def weather_fhour_key(source: str, region: str, cycle_iso: str, fhour: int) -> str:
    """Gzipped JSON wind grid for one forecast hour of one cycle.

    Example: ``weather:hrrr:conus:20260603T1200Z:f006``
    """
    return f"weather:{source}:{region}:{cycle_iso}:f{fhour:03d}"


def weather_manifest_key(source: str, region: str, cycle_iso: str) -> str:
    """JSON manifest: ``{cycle, reference_time, fhours, valid_times}``.

    Written last in ``ingest_cycle`` so its presence signals 'cycle is
    complete and consumable' to the forecast loader.
    """
    return f"weather:{source}:{region}:{cycle_iso}:manifest"


def weather_cycles_index_key(source: str, region: str) -> str:
    """Sorted set of cycle isos, score=cycle epoch.

    ``ZREVRANGE … 0 0`` returns the newest cycle. Trimmed to the most
    recent N entries by the ingest worker.
    """
    return f"weather:{source}:{region}:cycles"


def weather_latest_alias_key(source: str, region: str) -> str:
    """Backwards-compat alias preserving the original ``/api/weather``
    contract: holds the default-fhour blob for the newest cycle, no
    cycle id required to read it.
    """
    return f"weather:{source}:{region}:latest"


# ---------------------------------------------------------------------------
# Currents (NOAA OFS — FVCOM / ROMS unstructured-mesh outputs)


# Currents have separate nowcast ('n') and forecast ('f') runs. The
# Literal type catches a stray ``"forecast"`` at type-check time —
# would silently miss the manifest otherwise.
RunType = Literal["n", "f"]


def currents_topology_key(source: str) -> str:
    """Static mesh/grid blob for an OFS source. Independent of cycle
    and TTL'd long (mesh doesn't change inside a model release).

    Example: ``currents:lmhofs:topology``
    """
    return f"currents:{source}:topology"


def currents_snapshot_key(
    source: str, cycle_iso: str, run_type: RunType, fhour: int,
) -> str:
    """One fhour blob for either a nowcast or forecast run.

    Run-type letter precedes the fhour. Examples:

    * ``currents:lmhofs:20260603T0900Z:f006`` — forecast F006
    * ``currents:lmhofs:20260603T0900Z:n003`` — nowcast hour 3
    """
    return f"currents:{source}:{cycle_iso}:{run_type}{fhour:03d}"


def currents_manifest_key(source: str, cycle_iso: str, run_type: RunType) -> str:
    """JSON manifest per run-type. Nowcast + forecast are independent
    because they finish at different times within a cycle and the
    consumer merges them.

    Example: ``currents:lmhofs:20260603T0900Z:f_manifest``
    """
    return f"currents:{source}:{cycle_iso}:{run_type}_manifest"


def currents_cycles_index_key(source: str) -> str:
    """Sorted set of cycle isos for one OFS source. Shared between
    nowcast and forecast — both run types write to the same index.
    """
    return f"currents:{source}:cycles"


# ---------------------------------------------------------------------------
# Route compute / better-route notification


def route_cache_key(
    *,
    engine_version: str,
    race_id: Union[UUID, str],
    race_start: datetime,
    first_snapshot_ref: str,
    last_snapshot_valid: str,
    snapshot_sources: str,
    safety_factor: float,
    venue: Optional[str],
    derating_tag: str,
    currents_tag: str,
) -> str:
    """Compose the deterministic route-cache key.

    The shape is unchanged from the in-place f-string it replaces;
    centralising it here means a knob added to ``RouteRequest`` only
    needs the builder updated (and the pinning test re-recorded) for
    every consumer to pick it up.

    All inputs are positional-style kwargs so a refactor that reorders
    args at the call site cannot silently swap two fields. The pinning
    test in ``tests/test_redis_keys.py`` asserts the exact output.
    """
    return (
        f"route:{engine_version}:{race_id}:"
        f"{race_start.isoformat()}:"
        f"{first_snapshot_ref}:{last_snapshot_valid}:"
        f"{snapshot_sources}:{safety_factor:.2f}:venue={venue or '-'}:"
        f"{derating_tag}:currents={currents_tag}"
    )


def route_last_best_key(race_id: Union[UUID, str]) -> str:
    """Last total_minutes the background recompute either notified
    about or established silently. Used to decide whether the next
    recompute clears the improvement threshold.
    """
    return f"route:last_best:{race_id}"


def route_alternative_key(race_id: Union[UUID, str]) -> str:
    """The full alternative-route payload from the most recent
    better-route notification. SSE clients fetch it on connect so a
    reconnecting client doesn't miss state.
    """
    return f"route:alternative:{race_id}"


def route_notifications_channel(race_id: Union[UUID, str]) -> str:
    """Pub/sub channel the worker publishes to and the SSE endpoint
    subscribes on. *Not* a stored key — listed here so all route:*
    name choices live in one file.
    """
    return f"route:notifications:{race_id}"


def route_last_request_key(race_id: Union[UUID, str]) -> str:
    """The user's most recent ``RouteRequest`` JSON. The endpoint
    writes this on every successful compute; the background recompute
    reads it so its 'better route' alert is computed against the same
    derating / boat / safety_factor the user actually sees.

    Falling back to defaults when this key is missing is intentional
    (Redis flush resilience + races that haven't been opened since
    deploy). The worker logs the fallback so we can monitor.
    """
    return f"route:last_request:{race_id}"
