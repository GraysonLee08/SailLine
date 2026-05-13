# Session Summary — 2026-05-01

**Outcome:** Schema migration framework shipped *and* exercised end-to-end. Alembic 1.13.3 wired into the backend, prod stamped at `0001`, then `0002_add_track_points` applied through to production. Two real permissions failures along the way — both fixed in the moment and then hardened back into `infra/schema.sql` and `docs/migrations.md` so the next person doesn't repeat them. Yesterday's `CREATE TABLE IF NOT EXISTS` debugging spiral can't happen again.

---

## TL;DR

- ✅ Alembic + `psycopg[binary]` added to `backend/requirements.txt` (sync driver, used by the CLI only — runtime app keeps asyncpg)
- ✅ `backend/migrations/` scaffolded with `env.py`, template, and a baseline migration matching prod
- ✅ `infra/schema.sql` slimmed to bootstrap-only (PostGIS extension + role grants); all table DDL now lives in Alembic migrations
- ✅ Production database stamped at revision `0001`
- ✅ `0002_add_track_points.py` written, committed, applied to prod
- ✅ Cloud Run revision `sailline-api-00051-6p6` deployed; asyncpg pool flushed; `/health` verified
- ✅ `infra/schema.sql` and `docs/migrations.md` hardened with the lessons from 0002's first-attempt failure

---

## Files added or replaced

| Path | Status | Purpose |
|---|---|---|
| `backend/requirements.txt` | replaced | added `alembic==1.13.3` and `psycopg[binary]==3.2.3` |
| `backend/alembic.ini` | new | script location, sequential filename template, logging |
| `backend/migrations/env.py` | new | builds DB URL from env vars; sync migrations via psycopg |
| `backend/migrations/script.py.mako` | new | template used by `alembic revision -m ...` |
| `backend/migrations/versions/0001_baseline.py` | new | captures `user_profiles` + `race_sessions` as they exist in prod |
| `backend/migrations/versions/0002_add_track_points.py` | new | week 6 GPS recording table; applied to prod this session |
| `infra/schema.sql` | replaced (twice) | first pass: slimmed to bootstrap. Second pass: added `REFERENCES` to default privileges + replaced `GRANT ON ALL TABLES` with explicit table list |
| `docs/migrations.md` | new (revised) | workflow doc — corrected `--sql` advice, added Permissions section, added troubleshooting entries for the two failures we hit |

---

## Decisions worth noting

### Alembic over hand-rolled SQL versioning

The two real options were Alembic and "numbered SQL files with a version-tracking table." Alembic costs more to set up but pays back the first time we need history listing, downgrade scripts, or branching during a refactor. Numbered SQL files would have prevented yesterday's specific failure but would have been one project-internal tool away from where we are now. Alembic is the obvious Python-ecosystem choice and pip-installable in 30 seconds.

### Sync `psycopg` for migrations, not `asyncpg`

The runtime app uses asyncpg through the Cloud SQL Python Connector. Alembic supports async, but mixing async drivers with Alembic's CLI adds friction (event loop setup in `env.py`, `async def` migration functions) for no real benefit — migrations run from a shell, sequentially, once in a while. Sync `psycopg` is simpler and the migration code reads like the SQL it actually is.

### `infra/schema.sql` keeps doing one job

Split: `schema.sql` handles things that need superuser and rarely change (extension installation, role grants, `ALTER DEFAULT PRIVILEGES`). Alembic handles everything else. The new `schema.sql` adds `GRANT USAGE, CREATE ON SCHEMA public TO sailline` so the app user can create the `alembic_version` table and future migration tables without escalating to postgres.

### Manual workflow, no auto-apply in the deploy pipeline

Tempting to run `alembic upgrade head` as a Cloud Build deploy step, but a failed migration mid-deploy is a much messier failure mode than a known-state manual application. The runbook in `docs/migrations.md` is explicit enough that automation isn't urgent. Revisit after we've shipped 3–4 real migrations and have evidence of what failure modes actually occur.

---

## Production rollout — Part 1: framework setup

Executed from Cloud Shell (bash), with `cloud-sql-proxy` forwarding the private-IP Cloud SQL instance to `127.0.0.1:5432`:

```bash
cd ~/SailLine && git pull origin main

python3 -m venv ~/sailline-venv
source ~/sailline-venv/bin/activate
pip install alembic==1.13.3 'psycopg[binary]==3.2.3'

cloud-sql-proxy sailline:us-central1:sailline-db &

# Apply slimmed schema.sql as superuser (idempotent)
export PGPASSWORD=$(gcloud secrets versions access latest --secret=sailline-db-postgres-password)
psql -h 127.0.0.1 -U postgres -d sailline_app -f infra/schema.sql
unset PGPASSWORD

# Stamp the DB at 0001 as the app user
export DB_USER=sailline DB_NAME=sailline_app
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=sailline-db-app-password)
export DB_HOST=127.0.0.1 DB_PORT=5432
cd backend
alembic stamp 0001
alembic current   # → 0001
```

`alembic current` returned `0001` cleanly. Framework live.

---

## Production rollout — Part 2: applying 0002

After committing and pushing `0002_add_track_points.py`, applied it the same session.

```bash
cd ~/SailLine && git pull origin main
source ~/sailline-venv/bin/activate
cloud-sql-proxy sailline:us-central1:sailline-db &

export DB_USER=sailline DB_NAME=sailline_app
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=sailline-db-app-password)
export DB_HOST=127.0.0.1 DB_PORT=5432
cd backend

alembic upgrade head    # failed — see Issue #2 below
# ... fix ...
alembic upgrade head    # succeeded
alembic current         # → 0002 (head)
```

After the migration, verified the table exists and is owned correctly:

```sql
SELECT version_num FROM alembic_version;       -- 0002
\dt track_points
-- public | track_points | table | sailline
```

Then bumped the Cloud Run revision to flush the asyncpg pool:

```bash
gcloud run services update sailline-api \
    --region=us-central1 \
    --update-env-vars=BUMP=$(date +%s)
# → revision sailline-api-00051-6p6 serving 100% traffic
```

Smoke-tested `/health` — returned `{"status":"ok",...}`. No regression from the new revision.

---

## Issues hit (and fixes)

### 1. `alembic upgrade head --sql` regenerates everything from scratch

**What we saw:** Running `--sql` to preview the 0002 migration before applying produced output that included `CREATE TABLE alembic_version`, both 0001 tables, and 0002 — as if the database were empty.

**What was actually happening:** `--sql` is offline mode by design. It generates a deployable SQL script for handoff to a DBA, without connecting to any database, so it can't see the current revision and emits the entire migration history. Online `upgrade` (no `--sql`) reads the real `alembic_version` table and only runs the gap.

**Fix:** Stopped using `--sql`. The doc now recommends `alembic current` + `alembic history` + `alembic heads` to see the gap before applying — those *do* connect to the DB and reflect reality.

**Lesson:** Tool defaults aren't always what they seem. Read the actual behavior before trusting a "preview" mode.

### 2. `permission denied for table race_sessions` on first apply

**What we saw:** First real `alembic upgrade head` failed with `psycopg.errors.InsufficientPrivilege: permission denied for table race_sessions` while running the `CREATE TABLE track_points` statement. Transaction rolled back cleanly — `track_points` not partially created, `alembic_version` still at `0001`.

**Root cause:** Postgres requires the `REFERENCES` privilege on the **target** of a foreign key, not just the source. Our `infra/schema.sql` granted `SELECT, INSERT, UPDATE, DELETE` to `sailline` on the existing tables but not `REFERENCES`. The `track_points.session_id REFERENCES race_sessions(id)` line therefore failed because sailline couldn't reference race_sessions.

**Fix:** Added the missing grant manually as superuser, then re-ran the upgrade. Then folded the lesson back into `infra/schema.sql`:
- `ALTER DEFAULT PRIVILEGES` block now includes `REFERENCES` so future tables auto-grant it
- Catch-up grant on `user_profiles, race_sessions` now includes `REFERENCES`

**Lesson:** Default privileges only cover *future* tables — pre-Alembic tables need their grants explicitly. And the privilege list for FK-target tables needs `REFERENCES` in addition to DML.

### 3. `GRANT ON ALL TABLES` aborts on alembic_version

**What we saw:** First attempt to fix Issue #2 used `GRANT REFERENCES ON ALL TABLES IN SCHEMA public TO sailline` as superuser. Failed with `permission denied for table alembic_version`.

**Root cause:** `GRANT` requires ownership (or membership in the owning role), not just superuser. `alembic_version` was created by `sailline` when we ran `alembic stamp 0001`, so sailline owns it. The `ON ALL TABLES` form expands to "every table in the schema" and aborts the moment it hits one postgres can't grant on. This is mildly counterintuitive — superuser bypasses many privilege checks but not GRANT-on-non-owned-tables.

**Fix:** Listed tables explicitly: `GRANT REFERENCES ON user_profiles, race_sessions TO sailline`. Folded into `infra/schema.sql` with a long comment explaining why we list tables explicitly rather than using `ON ALL TABLES`.

**Lesson:** "ALL TABLES" forms become unsafe as soon as the schema contains tables with multiple owners. Once Alembic is in the mix, that's always.

---

## Lessons captured

All three issues are now documented in `docs/migrations.md` and the relevant fix is in `infra/schema.sql`:

- "Don't bother with `--sql` for preview" subsection in the Applying section, with the alternative commands
- "Permissions" section explaining ownership distinction and when superuser is needed
- Two new troubleshooting entries: `permission denied for table <pre-Alembic table>` (with the explicit-list GRANT command) and the offline `--sql` confusion

Future-self reading the doc has the context without having to dig through git history.

---

## Open items

**`infra/schema.sql` and `docs/migrations.md` need to be pushed.** The repo has the slimmed-bootstrap version of `schema.sql` from earlier this session; the corrected version with `REFERENCES` and explicit table list is sitting locally. Prod's permissions are correct (the manual `GRANT REFERENCES` we ran during the 0002 troubleshooting matches what the new schema.sql would do for a fresh DB). But anyone bootstrapping a new database from `main` right now would hit Issue #2. Push to close that gap:

```powershell
cd E:\Personal\Coding\SailLine
git add infra/schema.sql docs/migrations.md
git commit -m "Fix schema.sql: add REFERENCES grants, list legacy tables explicitly"
git push origin main
```

**No track_points consumer yet.** The table exists but no router or service queries it. That work is week 6 (GPS recording). For now it just sits there, costing nothing, ready when we need it.

---

## Next session priorities

Inheriting the 2026-04-30 list with item #1 now closed. In rough priority order:

1. ~~Schema migration framework~~ ✅ shipped this session
2. **Frontend deploy automation.** Cloud Build trigger that runs `npm run build && firebase deploy --only hosting` on push to `main`. Mirrors the existing backend trigger; ~15 min of work.
3. **Bundle splitting.** 2 MB first-load is a lot. Mapbox is the heavy hitter — lazy-load the `RaceEditor` route so the auth/list path stays light.
4. **Long-distance course presets** (Zimmer, Skipper's Club, Hammond, etc.) in `morfCourses.js`. Mark library supports them; just need the entries.
5. **Week 2 weather pipeline.** Technically "next" on the original schedule, independent work that can interleave with the cleanup above.

---

## Operational notes (additions for future reference)

- **Reusable Cloud Shell venv**: `~/sailline-venv` persists between Cloud Shell sessions. Just `source ~/sailline-venv/bin/activate` next time — no re-install needed.
- **Migration runbook**: full sequence in `docs/migrations.md` under "Apply against production." Copy-paste, adjust DB_USER if running as postgres for an extension/role change, run.
- **Don't use `alembic upgrade head --sql`**: offline mode regenerates everything. Use `alembic current` + `alembic history` + `alembic heads` to see the actual gap.
- **After applying any migration**: bump the Cloud Run revision to flush the asyncpg pool — `gcloud run services update sailline-api --region=us-central1 --update-env-vars=BUMP=$(date +%s)`. Same trick that fixed the stale-prepared-statement issue on 2026-04-30.
- **Permission errors on FK creation**: target table needs `REFERENCES` granted to sailline. For pre-Alembic tables, `GRANT REFERENCES ON user_profiles, race_sessions TO sailline` (don't use `ON ALL TABLES` — `alembic_version` is sailline-owned and the broad form aborts).
- **Tables created by Alembic are owned by sailline**: no follow-up GRANT needed for FKs between Alembic-created tables. The REFERENCES gymnastics only apply to FKs targeting the two pre-Alembic tables.
