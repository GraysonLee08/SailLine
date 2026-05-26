-- backfill_started_ended_at.sql
--
-- One-off backfill for race_sessions.started_at and ended_at after the
-- 2026-05-26 change that started writing these columns from the
-- telemetry ingest path.
--
-- Background: until 2026-05-26, no code wrote these columns even though
-- they existed in the schema since baseline 0001. Any race row created
-- before that change has started_at = NULL and ended_at = NULL despite
-- having a full track_points history and (often) a complete
-- mark_passes JSONB.
--
-- This script populates both columns from the authoritative sources
-- already in the database — no data is invented:
--
--   * started_at  ← race_sessions.start_at (the scheduled gun time)
--                   when start_at is non-NULL, ELSE the earliest
--                   track_points.recorded_at for that session
--                   (ad-hoc record with no scheduled start).
--
--   * ended_at    ← the timestamp of the FINAL mark_passes entry when
--                   the pass count equals the course length (race
--                   completed). Races that never completed
--                   (DNF / abandoned) keep ended_at = NULL.
--
-- Idempotent: WHERE clauses only touch rows where the target column is
-- currently NULL. Safe to re-run.
--
-- Run from Cloud SQL Studio, psql, or any GUI tool. Targets the
-- sailline_app database.
--
-- ─────────────────────────────────────────────────────────────────────
-- USAGE
--   1. (Recommended) Run the two SELECTs first to preview how many
--      rows will change.
--   2. Run the two UPDATEs.
--   3. Run the final SELECT to confirm no expected rows were missed.
-- ─────────────────────────────────────────────────────────────────────


-- 1a. Preview: how many rows would get started_at backfilled?
SELECT
    COUNT(*) FILTER (WHERE rs.start_at IS NOT NULL) AS will_use_gun_time,
    COUNT(*) FILTER (
        WHERE rs.start_at IS NULL
          AND EXISTS (
              SELECT 1 FROM track_points tp WHERE tp.session_id = rs.id
          )
    ) AS will_use_first_fix,
    COUNT(*) FILTER (
        WHERE rs.start_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM track_points tp WHERE tp.session_id = rs.id
          )
    ) AS skipped_no_data,
    COUNT(*) AS total_null_started_at
FROM race_sessions rs
WHERE rs.started_at IS NULL;


-- 1b. Backfill started_at.
--
-- Two-pass logic encoded in a single UPDATE via COALESCE:
--   * use start_at if it's populated (preferred — gun time semantics)
--   * else fall back to the earliest track_points.recorded_at
--   * if neither exists, leave NULL (will be SELECTed out below)
UPDATE race_sessions rs
SET
    started_at = COALESCE(
        rs.start_at,
        (SELECT MIN(tp.recorded_at) FROM track_points tp WHERE tp.session_id = rs.id)
    ),
    updated_at = NOW()
WHERE rs.started_at IS NULL
  AND (
      rs.start_at IS NOT NULL
      OR EXISTS (SELECT 1 FROM track_points tp WHERE tp.session_id = rs.id)
  );


-- 2a. Preview: how many rows would get ended_at backfilled?
--
-- Counts rows where mark_passes is a complete list of the course's
-- marks. Multi-lap courses with repeated marks in the marks array
-- still satisfy this because the pass count grows per lap.
SELECT COUNT(*) AS will_set_ended_at
FROM race_sessions rs
WHERE rs.ended_at IS NULL
  AND jsonb_array_length(rs.mark_passes) > 0
  AND jsonb_array_length(rs.mark_passes) = jsonb_array_length(rs.marks);


-- 2b. Backfill ended_at from the last entry in mark_passes.
--
-- The detector emits passes in chronological order, so the LAST entry
-- in mark_passes is the most recent pass timestamp. For a completed
-- race that timestamp is the final-mark rounding — exactly what
-- ended_at should record.
UPDATE race_sessions rs
SET
    ended_at = (
        rs.mark_passes -> (jsonb_array_length(rs.mark_passes) - 1) ->> 'ts'
    )::timestamptz,
    updated_at = NOW()
WHERE rs.ended_at IS NULL
  AND jsonb_array_length(rs.mark_passes) > 0
  AND jsonb_array_length(rs.mark_passes) = jsonb_array_length(rs.marks);


-- 3. Post-run audit: anything still NULL?
--
-- Rows that come back here are either:
--   * Races with no track_points and no start_at (created and abandoned
--     pre-recording) — started_at stays NULL.
--   * Races that never completed the course (DNF / abandoned mid-race)
--     — ended_at stays NULL.
-- Both are correct end-states; no further action needed.
SELECT
    rs.id,
    rs.name,
    rs.created_at,
    rs.start_at,
    rs.started_at,
    rs.ended_at,
    jsonb_array_length(rs.marks) AS course_len,
    jsonb_array_length(rs.mark_passes) AS pass_count,
    (SELECT COUNT(*) FROM track_points tp WHERE tp.session_id = rs.id) AS point_count
FROM race_sessions rs
WHERE rs.started_at IS NULL OR rs.ended_at IS NULL
ORDER BY rs.created_at DESC
LIMIT 50;
