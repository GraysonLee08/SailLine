# Post-race stats — multi-session plan (D1 / D2 / D3)

**Date:** 2026-05-14
**Supersedes the "Session D" entry in `2026-05-14_race-tracking-improvements-plan.md`.** That doc described stats only. Real scope (post live-test feedback) is larger and splits cleanly into three sessions.

---

## Session D1 — Stats view + AI summary + wind snapshot

**Goal:** Ship a usable post-race screen with computed stats, a Claude-generated recap + coaching tips, and a frozen wind snapshot so wind-vs-track analysis works regardless of how old the race is. Lays the data-model groundwork that D2 and D3 build on.

### Architecture

```
Auto-stop fires (last mark rounded)
        │
        ▼
tracks.py detects final-mark pass
        │
        ▼
Fire-and-forget invoke: Cloud Run Job `race-postprocess`
        │
        ├── compute stats (services/race_stats.py)
        ├── snapshot wind   (services/wind_snapshot.py)
        └── generate summary (services/race_summary.py)
        │
        ▼
UPDATE race_sessions
   SET ai_summary = ..., wind_snapshot = ...
        │
        ▼
GET /api/races/{id}/stats  ── pure read; stats compute is cached in Redis
```

### Backend deliverables

- **Migration 0009** — `ai_summary JSONB NULL` on `race_sessions`. Shape: `{recap, tips, model, prompt_version, generated_at}`.
- **Migration 0010** — `wind_snapshot JSONB NULL` on `race_sessions`. Shape: `{bbox, grid_deg, t_start, t_end, dt_s, samples: [[t_idx, lat, lon, dir_deg, speed_kt]]}`. Compressed via gzip-base64 if >50 KB (we'll measure first).
- `app/services/race_stats.py` — pure function: `compute_stats(track_points, marks, mark_passes) -> RaceStats`. Includes Douglas-Peucker downsample for the speed series.
- `app/services/wind_snapshot.py` — pure function: `snapshot_forecast(forecast, marks_bbox, t_start, t_end) -> dict`. Reads from the existing WindForecast (Redis-cached); writes a self-contained blob.
- `app/services/race_summary.py` — Anthropic call with `claude-haiku-4-5-20251001`. `PROMPT_VERSION = 1`. Prompt includes stats summary + wind context. Returns `{recap, tips, ...}` or None on failure.
- `workers/race_postprocess.py` — new Cloud Run Job. Idempotent: skip if ai_summary already present unless `--force`.
- `app/routers/tracks.py` — fire-and-forget trigger when `len(mark_passes) == len(marks)`.
- `app/routers/race_stats.py` — `GET /api/races/{id}/stats`, `POST /api/races/{id}/stats/regenerate` (pro-gated).
- `requirements.txt` — uncomment `anthropic`. `ANTHROPIC_API_KEY` added to Secret Manager.

### Frontend deliverables

- **MapView refactor** — pull existing rendering into `<MarksLayer>`, `<WindBarbsLayer>`, `<RouteLayer>`. New `<TrackLayer>` for read-only polyline. MapView becomes a shell.
- `hooks/useRaceStats.js` — fetches `/api/races/:id/stats`, exposes `{stats, summary, loading, error, regenerate}`.
- `RaceStatsView.jsx` — full-screen view: header tiles (distance, elapsed, avg/max SOG), AI summary card (recap + tips), leg-by-leg table, speed sparkline, read-only map.
- `RacesListView.jsx` — "View stats" entry for races with `mark_passes.length > 0`.
- Auto-stop hook → on `stop()`, navigate to the new view.

### What this session deliberately defers

- Sharing the stats with anyone other than the race owner (auth stays user_id-keyed). The summary lives on the race row, so D3 only needs to widen the auth check, not move the data.
- Boat-level identity. Stats reference the race's `boat_class` string; corrected time waits for D2.
- AIS/fleet comparison. The page is laid out with corrected-time in mind so it slots in.

### Tech debt to flag (this session)

- `race_stats` route returns stats even if the Cloud Run Job hasn't finished — the summary card shows a skeleton until the row gets backfilled. Acceptable; we don't want to block stats on the LLM.
- Wind snapshot grid resolution is hard-coded (10 km base, native for venues). Per-race tuning is a follow-up if blob size or interpolation accuracy bites.
- Speed-series Douglas-Peucker epsilon hard-coded. Configurable later if courses with very different scales need different fidelity.

---

## Session D2 — Boat profile + PHRF handicap

**Goal:** Boats become first-class entities. Owners can record their PHRF certificate (manual entry first, optional PDF upload). Stats show corrected time alongside elapsed.

### Scope

- New `boats` table:

  ```
  id, owner_id, name, sail_number, yacht_type, year, mwphrf_region,
  loa, lwl, beam, draft, displacement,
  engine, prop_install, prop_type,
  p, e, i, j, isp, spl, jc_tps,
  hcp, dhcp, nshcp, dnshcp,                  -- the four handicaps
  cert_pdf_gcs_url, cert_issued_on,
  created_at, updated_at
  ```

- `race_sessions.boat_id` — nullable FK so existing races still load.
- Boat management UI: list, create, edit, delete, optional cert PDF upload (stored in GCS, no parsing v1).
- Stats view shows **corrected time** computed from the boat's handicap and the course shape: use `hcp` (buoy) when the course is a closed buoy loop, `dhcp` (random leg) otherwise. Display next to elapsed time.
- Backfill path: leave `boat_class` string on `race_sessions` for legacy races; populate `boat_id` when present and prefer it.
- **PHRF source-of-truth question:** MWPHRF maintains a database of certificates. Loading it is a yearly/season import — flag as a follow-up (D2.5). For v1, owners enter their own rating from their cert; we trust them.

### Tech debt to flag (forward)

- Manual entry means no validation against the actual MWPHRF database. A scheduled import job (`workers/phrf_import.py`) is the v2.
- Cert PDF is stored but not parsed. The cert structure (MWPHRF 2026 template, fixed-position fields) is regex-friendly — parser is a small follow-up.
- Other handicap systems (ORR, ORC, IRC, fleet-local) are out of scope for v1. Schema is generic-handicap shaped enough that adding columns or a `handicap_system` enum + JSON of values is a small migration if needed.

---

## Session D3 — Sharing + crew

**Goal:** A boat owner can invite crew; crew see the boat's races on their profile and can read (not edit) stats + summary.

### Scope

- New `boat_crew` table — `(boat_id, user_id, role, joined_at)` where role is `owner | crew | viewer`.
- Invite flow: owner generates a join code or sends an invite by email; recipient accepts and gets a `boat_crew` row.
- Auth refactor: every race-scoped endpoint changes from `user_id == :uid` to `EXISTS (SELECT 1 FROM boat_crew WHERE boat_id = :bid AND user_id = :uid)`. This touches `races.py`, `tracks.py`, `routing.py`, `race_stats.py`, `routing_notifications.py`.
- Write vs read split: only `owner` can edit the race; `crew` and `viewer` can read stats, summary, track.
- "Shared with you" filter on RacesListView.

### Tech debt to flag (forward)

- The auth refactor is the biggest risk in this session — it's a cross-cutting change. Plan a single PR with the migration + the auth-helper change + every route at once.
- Race history that pre-dates D2 has no `boat_id`, so it can't be shared. Owners stay the only viewer for legacy races; UX explains this.

---

## Sequencing notes

- D1 must land **before** D2 because D2 changes the stats endpoint's response (corrected time field). Easier to add a new field than to change the contract while consumers exist.
- D2 must land **before** D3 because D3's auth predicate JOINs through `boats`.
- All three should ship behind feature flags if Capacitor releases land in between — a sharing bug in a shipped iOS build is harder to roll back than a flag flip.
