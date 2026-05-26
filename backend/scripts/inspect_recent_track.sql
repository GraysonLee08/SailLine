-- inspect_recent_track.sql
--
-- Pull captured GPS data from the most recent race session for the
-- signed-in user. Used to verify the mobile recorder after a smoke test:
--
--   * Did points actually get persisted?
--   * Are they ~1 Hz (cadence target) or sparse?
--   * Any multi-minute gaps (battery-optimisation / OS-suspended)?
--   * Did the auto-start fire when expected (started_at vs scheduled gun)?
--
-- Run from the backend/ venv with the prod or dev DB URL set:
--
--   PowerShell:
--     $env:PGPASSWORD = "<password>"
--     psql -h 127.0.0.1 -U sailline -d sailline_app `
--          -v user_id="'<firebase-uid>'" `
--          -f scripts/inspect_recent_track.sql
--
--   To target a specific session instead of "most recent", set
--   -v session_id="'<uuid>'" and the WHERE clauses pick it up.
--
-- If you don't pass either variable, psql will warn about unbound vars;
-- pass at minimum :user_id.

-- ── psql parameter defaults ────────────────────────────────────────────
-- These let the script run without -v by leaving the filters open. In
-- practice always pass :user_id so you don't pull someone else's track.
\set ON_ERROR_STOP on
\set user_id    NULL
\set session_id NULL

-- ── 1. Identify the target session ─────────────────────────────────────
--
-- Picks the row matching :session_id if given, else the most recent
-- race_session for :user_id, else the most recent session anywhere
-- (dev/local convenience — never what you want in prod).
WITH target AS (
    SELECT id
    FROM race_sessions
    WHERE (:'session_id' IS NULL OR id = :'session_id'::uuid)
      AND (:'user_id'    IS NULL OR user_id = :'user_id')
    ORDER BY created_at DESC
    LIMIT 1
)
SELECT
    rs.id                AS session_id,
    rs.name,
    rs.mode,
    rs.boat_class,
    rs.started_at,
    rs.ended_at,
    rs.created_at,
    jsonb_array_length(rs.marks) AS mark_count
FROM race_sessions rs
JOIN target t ON t.id = rs.id;

-- ── 2. Track summary stats ─────────────────────────────────────────────
--
-- One row. The headline numbers you want to see after a 15-minute
-- screen-locked smoke test: point count, span, expected vs actual cadence,
-- worst gap.
WITH target AS (
    SELECT id
    FROM race_sessions
    WHERE (:'session_id' IS NULL OR id = :'session_id'::uuid)
      AND (:'user_id'    IS NULL OR user_id = :'user_id')
    ORDER BY created_at DESC
    LIMIT 1
),
gaps AS (
    SELECT
        tp.recorded_at,
        EXTRACT(EPOCH FROM (
            tp.recorded_at - LAG(tp.recorded_at)
                OVER (ORDER BY tp.recorded_at)
        )) AS gap_sec
    FROM track_points tp
    JOIN target t ON t.id = tp.session_id
)
SELECT
    COUNT(*)                                  AS point_count,
    MIN(recorded_at)                          AS first_fix_at,
    MAX(recorded_at)                          AS last_fix_at,
    EXTRACT(EPOCH FROM (MAX(recorded_at)
                      - MIN(recorded_at)))    AS span_sec,
    ROUND(AVG(gap_sec)::numeric, 2)           AS avg_gap_sec,
    ROUND(MAX(gap_sec)::numeric, 2)           AS max_gap_sec,
    COUNT(*) FILTER (WHERE gap_sec > 5)       AS gaps_over_5s,
    COUNT(*) FILTER (WHERE gap_sec > 30)      AS gaps_over_30s,
    COUNT(*) FILTER (WHERE gap_sec > 120)     AS gaps_over_2min
FROM gaps;

-- ── 3. The largest gaps, ordered worst-first ──────────────────────────
--
-- If max_gap_sec above looks bad, this tells you exactly when. Use the
-- timestamps to correlate against what was happening on the boat (e.g.
-- went below decks, phone screen locked, switched to camera, etc.)
WITH target AS (
    SELECT id
    FROM race_sessions
    WHERE (:'session_id' IS NULL OR id = :'session_id'::uuid)
      AND (:'user_id'    IS NULL OR user_id = :'user_id')
    ORDER BY created_at DESC
    LIMIT 1
),
gaps AS (
    SELECT
        tp.recorded_at AS gap_ended_at,
        LAG(tp.recorded_at)
            OVER (ORDER BY tp.recorded_at) AS gap_started_at,
        EXTRACT(EPOCH FROM (
            tp.recorded_at - LAG(tp.recorded_at)
                OVER (ORDER BY tp.recorded_at)
        )) AS gap_sec
    FROM track_points tp
    JOIN target t ON t.id = tp.session_id
)
SELECT
    gap_started_at,
    gap_ended_at,
    ROUND(gap_sec::numeric, 2) AS gap_sec
FROM gaps
WHERE gap_sec > 5
ORDER BY gap_sec DESC
LIMIT 10;

-- ── 4. Sample of actual track points (first 5, last 5) ────────────────
--
-- Useful to eyeball the path: are speeds plausible, headings sane, GPS
-- accuracy reasonable? `position` is GEOGRAPHY — we ST_X/ST_Y it for
-- readable lon/lat.
WITH target AS (
    SELECT id
    FROM race_sessions
    WHERE (:'session_id' IS NULL OR id = :'session_id'::uuid)
      AND (:'user_id'    IS NULL OR user_id = :'user_id')
    ORDER BY created_at DESC
    LIMIT 1
),
numbered AS (
    SELECT
        tp.recorded_at,
        ROUND(ST_Y(tp.position::geometry)::numeric, 6) AS lat,
        ROUND(ST_X(tp.position::geometry)::numeric, 6) AS lon,
        ROUND(tp.speed_kts::numeric, 2)                AS speed_kts,
        ROUND(tp.heading_deg::numeric, 1)              AS heading_deg,
        ROW_NUMBER() OVER (ORDER BY tp.recorded_at)         AS rn_asc,
        ROW_NUMBER() OVER (ORDER BY tp.recorded_at DESC)    AS rn_desc,
        COUNT(*)    OVER ()                                 AS total
    FROM track_points tp
    JOIN target t ON t.id = tp.session_id
)
SELECT
    recorded_at,
    lat,
    lon,
    speed_kts,
    heading_deg,
    CASE WHEN rn_asc <= 5 THEN 'first' ELSE 'last' END AS sample
FROM numbered
WHERE rn_asc <= 5 OR rn_desc <= 5
ORDER BY recorded_at;

-- ── 5. Total distance covered (sanity check) ──────────────────────────
--
-- Sum of haversine distances between consecutive fixes, in nautical
-- miles. Sanity-check against your mental model of how far you actually
-- went during the smoke test.
WITH target AS (
    SELECT id
    FROM race_sessions
    WHERE (:'session_id' IS NULL OR id = :'session_id'::uuid)
      AND (:'user_id'    IS NULL OR user_id = :'user_id')
    ORDER BY created_at DESC
    LIMIT 1
),
legs AS (
    SELECT
        ST_Distance(
            tp.position,
            LAG(tp.position) OVER (ORDER BY tp.recorded_at)
        ) AS leg_m
    FROM track_points tp
    JOIN target t ON t.id = tp.session_id
)
SELECT
    ROUND((SUM(leg_m) / 1852.0)::numeric, 3) AS distance_nm,
    ROUND((SUM(leg_m) / 1000.0)::numeric, 3) AS distance_km
FROM legs;
