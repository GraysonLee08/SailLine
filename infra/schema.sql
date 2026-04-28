-- infra/schema.sql
-- Initial schema for SailLine. Idempotent — safe to re-run.
-- Apply from Cloud Shell:
--   gcloud sql connect sailline-db --user=postgres --database=sailline_app < infra/schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS user_profiles (
    id              TEXT PRIMARY KEY,           -- Firebase Auth UID
    tier            TEXT NOT NULL DEFAULT 'free'
                    CHECK (tier IN ('free', 'pro', 'hardware')),
    stripe_id       TEXT UNIQUE,
    boat_class      TEXT,
    handicap_system TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Tables for race_sessions, track_points, telemetry_points are added in
-- their respective build weeks (see architecture.md §9).
-- at the bottom of infra/schema.sql, before the final newline
GRANT SELECT, INSERT, UPDATE, DELETE ON user_profiles TO sailline;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sailline;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sailline;