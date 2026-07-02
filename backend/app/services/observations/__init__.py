"""Post-race observed-conditions snapshots ("actuals").

The wind_snapshot module freezes what the *forecast* said; this package
freezes what actually *happened*, as measured by real instruments near
the racecourse — buoys, C-MAN shore stations, and (later) tide/current
stations.

Provider-agnostic by design: ``base.ObservationProvider`` is the
protocol, ``ndbc.NdbcProvider`` is provider #1 (covers the Great Lakes
AND every US coast through one feed). Adding NOAA CO-OPS water
levels/currents later means one new provider class — the snapshot
schema, the postprocess wiring, and the DB column don't change.
"""
from app.services.observations.base import (
    ObservationProvider,
    build_obs_snapshot,
    default_providers,
)
from app.services.observations.ndbc import NdbcProvider

__all__ = [
    "ObservationProvider",
    "NdbcProvider",
    "build_obs_snapshot",
    "default_providers",
]
