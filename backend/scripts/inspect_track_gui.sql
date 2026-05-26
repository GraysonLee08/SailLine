-- inspect_track_gui.sql
--
-- GUI-friendly version of inspect_recent_track.sql for Cloud SQL Studio,
-- pgAdmin, DBeaver, etc. — anything that does NOT support psql backslash
-- meta-commands (\set, \i, \v).
--
-- The session ID is hard-coded in the placeholder below. Find/replace
-- it with the session you want to inspect, then run the queries one at
-- a time (most GUI tools won't run all 5 in a single execution).
--
-- Find the session ID with:
--   SELECT id, name, started_at FROM race_sessions
--   WHERE user_id = '<your-firebase-uid>'
--   ORDER BY created_at DESC LIMIT 5;

-- ─────────────────────────────────────────────────────────────────────
-- REPLACE THIS UUID before running:
--   '6f7a2111-d7c0-48d7-8884-f09619f5cdf6'
-- ─────────────────────────────────────────────────────────────────────


-- 1. Session row + basic metadata
SELECT id, name, mode, boat_class, started_at, ended_at, created_at,
       jsonb_array_length(marks) AS mark_count
FROM race_sessions
WHERE id = '6f7a2111-d7c0-48d7-8884-f09619f5cdf6';


-- 2. Summary stats — point count, span, cadence, gap counts
WITH gaps AS (
    SELECT recorded_at,
           EXTRACT(EPOCH FROM (
               recorded_at - LAG(recorded_at) OVER (ORDER BY recorded_at)
           )) AS gap_sec
    FROM track_points
    WHERE session_id = '6f7a2111-d7c0-48d7-8884-f09619f5cdf6'
)
SELECT COUNT(*)                                  AS point_count,
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


-- 3. Top 10 worst gaps (correlate timestamps against what was happening)
WITH gaps AS (
    SELECT recorded_at AS gap_ended_at,
           LAG(recorded_at) OVER (ORDER BY recorded_at) AS gap_started_at,
           EXTRACT(EPOCH FROM (
               recorded_at - LAG(recorded_at) OVER (ORDER BY recorded_at)
           )) AS gap_sec
    FROM track_points
    WHERE session_id = '6f7a2111-d7c0-48d7-8884-f09619f5cdf6'
)
SELECT gap_started_at,
       gap_ended_at,
       ROUND(gap_sec::numeric, 2) AS gap_sec
FROM gaps
WHERE gap_sec > 5
ORDER BY gap_sec DESC
LIMIT 10;


-- 4. First 5 and last 5 fixes — sanity-check the path
WITH numbered AS (
    SELECT recorded_at,
           ROUND(ST_Y(position::geometry)::numeric, 6) AS lat,
           ROUND(ST_X(position::geometry)::numeric, 6) AS lon,
           ROUND(speed_kts::numeric, 2)                AS speed_kts,
           ROUND(heading_deg::numeric, 1)              AS heading_deg,
           ROW_NUMBER() OVER (ORDER BY recorded_at)         AS rn_asc,
           ROW_NUMBER() OVER (ORDER BY recorded_at DESC)    AS rn_desc
    FROM track_points
    WHERE session_id = '6f7a2111-d7c0-48d7-8884-f09619f5cdf6'
)
SELECT recorded_at,
       lat,
       lon,
       speed_kts,
       heading_deg,
       CASE WHEN rn_asc <= 5 THEN 'first' ELSE 'last' END AS sample
FROM numbered
WHERE rn_asc <= 5 OR rn_desc <= 5
ORDER BY recorded_at;


-- 5. Total distance covered (haversine on GEOGRAPHY)
WITH legs AS (
    SELECT ST_Distance(
               position,
               LAG(position) OVER (ORDER BY recorded_at)
           ) AS leg_m
    FROM track_points
    WHERE session_id = '6f7a2111-d7c0-48d7-8884-f09619f5cdf6'
)
SELECT ROUND((SUM(leg_m) / 1852.0)::numeric, 3) AS distance_nm,
       ROUND((SUM(leg_m) / 1000.0)::numeric, 3) AS distance_km
FROM legs;
