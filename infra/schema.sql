-- infra/schema.sql
--
-- One-time bootstrap for a SailLine database. Run this BEFORE running
-- Alembic migrations on a fresh database. Idempotent — safe to re-run.
--
-- What lives here:
--   - PostGIS extension
--   - Default privileges so future tables auto-grant to the `sailline` user
--   - Catch-up grants on any existing tables (handles the prod DB)
--
-- What does NOT live here (table DDL):
--   - All `CREATE TABLE`, `CREATE INDEX`, and column changes go through
--     Alembic migrations under `backend/migrations/versions/`. See
--     `docs/migrations.md`. The whole reason Alembic exists in this repo
--     is that `CREATE TABLE IF NOT EXISTS` here silently no-op'd against
--     a drifted prod schema and cost us a debug session on 2026-04-30.
--
-- Apply from Cloud Shell as the `postgres` superuser:
--   gcloud sql connect sailline-db --user=postgres --database=sailline_app < infra/schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;

-- Default privileges: tables created hereafter (by Alembic, by hand,
-- whoever) automatically grant DML to the app user. This is what lets
-- migrations run as `sailline` without a follow-up GRANT step.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sailline;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sailline;

-- Schema-level: sailline needs CREATE on `public` so Alembic can create
-- new tables and the `alembic_version` tracking table.
GRANT USAGE, CREATE ON SCHEMA public TO sailline;

-- Catch-up grants for any tables that already exist. DEFAULT PRIVILEGES
-- only affects future objects, so existing tables (user_profiles,
-- race_sessions on the prod DB) need their grants applied directly.
-- Idempotent — re-running just re-grants what's already granted.
DO $$
BEGIN
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO sailline';
    EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sailline';
END
$$;
