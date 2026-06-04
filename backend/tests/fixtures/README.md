# Test fixtures

Real-world data used by the v3 mark-rounding tests and any other tests
that need a known-good real GPS trace.

## beer_can_race_4_20260603.json

Mobile-recorder telemetry from the 2026-06-03 MORF Beer Can Race 4
(Chicago, Lake Michigan). 4,543 GPS points at 1 Hz, course = 6 marks
(SA7 → 3 → 2 → 8 → 7 → SA7).

The fixture motivated the cross-batch detector-state persistence work
(migration 0020, 2026-06-04). With per-batch state resets and a
1-sample-per-batch upload cadence, the v3 detector missed Mark 2
despite a textbook 1.1 m closest-point-of-approach. The
``test_streaming_one_sample_batches_detects_all_marks`` test replays
this trace exactly that way (one ``feed_batch`` call per sample, with
``dump_state`` / ``restore_state`` between calls) and asserts every
mark is detected.

Race ended in DNF — only the first four marks were actually sailed,
then the boat motored back. The fixture tests assert detection of
the first three auto-detectable marks (SA7 start, Mark 3, Mark 2) and
Mark 8; the last two are unreachable and stay undetected, matching
production behaviour.

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
