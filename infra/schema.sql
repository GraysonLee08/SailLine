-- infra/schema.sql
-- Initial schema for SailLine. Idempotent — safe to re-run.
-- Apply from Cloud Shell:
--   gcloud sql connect sailline-db --user=postgres --database=sailline_app < infra/schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------------------
-- Auth + user profiles

CREATE TABLE IF NOT EXISTS user_profiles (
    id              TEXT PRIMARY KEY,           -- Firebase Auth UID
    tier            TEXT NOT NULL DEFAULT 'free'
                    CHECK (tier IN ('free', 'pro', 'hardware')),
    stripe_id       TEXT UNIQUE,
    boat_class      TEXT,
    handicap_system TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Race setup (Week 3)
--
-- One row per race the user creates. The `course` JSONB column holds the
-- marks registry, the lap sequence, and lap count — all validated by
-- Pydantic on the way in (see backend/app/models/race.py).
--
-- boat_class has no CHECK constraint here on purpose: adding a boat class
-- should be a Python deploy, not a SQL migration. The Pydantic enum is the
-- source of truth.

CREATE TABLE IF NOT EXISTS race_sessions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     TEXT NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('inshore', 'distance')),
    boat_class  TEXT NOT NULL,
    course      JSONB NOT NULL,
    started_at  TIMESTAMPTZ,
    ended_at    TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS race_sessions_user_created_idx
    ON race_sessions (user_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Grants
--
-- Run as the schema owner (`postgres`) once per database.
-- Future tables auto-grant via the DEFAULT PRIVILEGES statements below.

GRANT SELECT, INSERT, UPDATE, DELETE ON user_profiles  TO sailline;
GRANT SELECT, INSERT, UPDATE, DELETE ON race_sessions  TO sailline;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sailline;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sailline;

-- track_points and telemetry_points land in their respective build weeks
-- (see architecture.md §9).
