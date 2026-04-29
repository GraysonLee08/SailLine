"""Parse NOAA GRIB2 files into 10m wind grids."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import xarray as xr


@dataclass(frozen=True)
class WindGrid:
    """10m above-ground wind components on a lat/lon grid.

    Conventions:
      lats: 1D, degrees (typically 90 -> -90 for GFS)
      lons: 1D, degrees, sorted ascending in -180..180
      u, v: 2D shape (len(lats), len(lons)), m/s — eastward / northward
    """
    lats: np.ndarray
    lons: np.ndarray
    u: np.ndarray
    v: np.ndarray
    reference_time: datetime  # forecast issue time (UTC)
    valid_time: datetime      # forecast valid time (UTC)
    source: str               # "gfs" or "hrrr"


def parse_grib_to_wind_grid(path: str | Path, source: str = "gfs") -> WindGrid:
    """Read a GRIB2 file and extract 10m wind components.

    Expects a single forecast hour. Multi-hour files aren't supported here —
    the ingest worker should fetch one hour per file.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    ds = xr.open_dataset(
        path,
        engine="cfgrib",
        filter_by_keys={"typeOfLevel": "heightAboveGround", "level": 10},
        backend_kwargs={"indexpath": ""},  # don't write .idx cache next to data
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

    # GFS publishes lons in 0..360. Normalize to -180..180 and sort ascending
    # so bbox slicing downstream is trivial.
    if lons.max() > 180:
        lons = np.where(lons > 180, lons - 360, lons)
        order = np.argsort(lons)
        lons = lons[order]
        u = u[:, order]
        v = v[:, order]

    return WindGrid(
        lats=lats, lons=lons, u=u, v=v,
        reference_time=ref, valid_time=valid, source=source,
    )


def _to_datetime(np_dt) -> datetime:
    """Convert a numpy datetime64 scalar to a tz-aware Python datetime (UTC)."""
    ts = np.datetime64(np_dt, "s").astype("int64")
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)