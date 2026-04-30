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

-- Race plans + (eventually) live/completed races. v1 uses this for pre-race
-- planning only; started_at / ended_at stay NULL until in-race mode lands
-- in week 6. `name` is added beyond the architecture.md spec for the list UI.
CREATE TABLE IF NOT EXISTS race_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('inshore', 'distance')),
    boat_class  TEXT NOT NULL,
    marks       JSONB NOT NULL DEFAULT '[]'::jsonb,
    started_at  TIMESTAMPTZ,
    ended_at    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS race_sessions_user_idx
    ON race_sessions(user_id, created_at DESC);

-- Grant the app user (`sailline`) access to existing and future tables.
-- These statements run as the schema owner (`postgres`) and only need to
-- happen once per database. Future tables auto-grant via DEFAULT PRIVILEGES.
GRANT SELECT, INSERT, UPDATE, DELETE ON user_profiles TO sailline;
GRANT SELECT, INSERT, UPDATE, DELETE ON race_sessions TO sailline;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sailline;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sailline;

-- Tables for track_points, telemetry_points are added in their respective
-- build weeks (see architecture.md §9).
