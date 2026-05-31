# Test fixtures

Real-world data used by the v3 mark-rounding tests and any other tests
that need a known-good real GPS trace.

## colors_bravo_20260530.gpx

Garmin Connect GPX export from the 2026-05-30 Cook County Sailing
distance race ("Colors (Bravo)"). 2,285 GPS points over 2 h 28 min,
sampled every 5 s, covering the full course: Start → CCYC E2 → CCYC T
→ Wilson Crib → Harrison-Dever Crib → Purdue Met Buoy 1A → Finish.

This race exposed the v2 detector's fixed-radius failure mode — it
recorded zero `mark_passes` because the boat passed nav structures at
100-300 m off. The v3 streaming-CPA algorithm (250 m distance-mode
threshold) catches all seven marks. See
`sailline-docs/2026-05-30_session.md` for the post-mortem.

## gfs_10m_wind_sample.grib2 / hrrr_10m_wind_sample.grib2

NOAA forecast samples used by the wind-snapshot ingest tests.

## mwphrf_gaucho.pdf

A sanitised MWPHRF rating cert used to test PDF parsing on the boats
endpoint.
