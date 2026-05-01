-- infra/schema.sql
--
-- One-time bootstrap for a SailLine database. Run this BEFORE running
-- Alembic migrations on a fresh database. Idempotent — safe to re-run.
--
-- What lives here:
--   - PostGIS extension
--   - Default privileges so future tables auto-grant to the `sailline` user
--   - Catch-up grants on the pre-Alembic tables (user_profiles, race_sessions)
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
-- Or via cloud-sql-proxy + psql:
--   psql -h 127.0.0.1 -U postgres -d sailline_app -f infra/schema.sql

CREATE EXTENSION IF NOT EXISTS postgis;

-- Default privileges: tables created hereafter (by Alembic, by hand,
-- whoever) automatically grant DML + REFERENCES to the app user.
--
-- REFERENCES is needed whenever a new table adds a foreign key pointing
-- at an existing table — Postgres requires REFERENCES on the *target* of
-- a FK, not just the source. Missing this is what caused the 0002
-- migration's first apply to fail with `permission denied for table
-- race_sessions` on 2026-05-01.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES ON TABLES TO sailline;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sailline;

-- Schema-level: sailline needs CREATE on `public` so Alembic can create
-- new tables and the `alembic_version` tracking table.
GRANT USAGE, CREATE ON SCHEMA public TO sailline;

-- Catch-up grants for the pre-Alembic tables. DEFAULT PRIVILEGES only
-- affects future objects, so the two tables that existed before Alembic
-- was wired up (user_profiles, race_sessions) need their grants applied
-- directly. Both are owned by `postgres`.
--
-- Listing tables explicitly rather than using `GRANT ON ALL TABLES IN
-- SCHEMA public` is deliberate: GRANT requires ownership, and the
-- `alembic_version` table is owned by `sailline` (created by Alembic
-- the first time it ran). The ALL form fails with `permission denied
-- for table alembic_version` even when run as superuser, because
-- "all tables" includes tables postgres can't grant on.
--
-- New tables created by Alembic going forward are owned by `sailline`
-- and need no explicit GRANT — sailline already has every privilege
-- on tables it owns.
GRANT SELECT, INSERT, UPDATE, DELETE, REFERENCES
    ON user_profiles, race_sessions
    TO sailline;

-- Ownership catch-up: explicitly set the owner to sailline for tables
-- that already exist but were created by the `postgres` user before Alembic
-- took over. This fixes the permission errors on subsequent migration applies.
ALTER TABLE IF EXISTS race_sessions OWNER TO sailline;
ALTER TABLE IF EXISTS user_profiles OWNER TO sailline;