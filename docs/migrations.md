# Schema Migrations

SailLine uses [Alembic](https://alembic.sqlalchemy.org/) to manage Postgres schema changes. Every change to a table — new column, new index, new table — goes through a migration file in `backend/migrations/versions/` so we have a versioned, reviewable history that applies the same way every time.

`infra/schema.sql` only handles one-time bootstrap (PostGIS extension, role grants). Don't add tables there.

---

## Why Alembic (and what it fixes)

The pre-Alembic workflow used `CREATE TABLE IF NOT EXISTS` in `infra/schema.sql`. That makes re-runs idempotent but blind to schema drift — if a table already exists with a different shape, the script silently no-ops. We hit this exact failure on 2026-04-30 (see `docs/2026-04-30-session-summary.md`): the script reported `CREATE TABLE` and exited cleanly while the table on disk still had the old columns. Three re-applies before we caught it.

Alembic tracks applied revisions in a dedicated `alembic_version` table. Migrations fail loudly when something is wrong, and `--sql` mode lets us review the exact SQL before running it.

---

## File layout

```
backend/
├── alembic.ini                    # config: script location, file template, logging
└── migrations/
    ├── env.py                     # builds DB URL from env vars; runs migrations
    ├── script.py.mako             # template for new migration files
    └── versions/
        └── 0001_baseline.py       # captures user_profiles + race_sessions
```

All `alembic` commands must be run from the `backend/` directory (where `alembic.ini` lives).

---

## One-time setup

### Local development

The deps are already in `requirements.txt`. Install and run against a local Postgres or via `cloud-sql-proxy`:

```bash
cd backend
pip install -r requirements.txt

# Point at your DB. Local Postgres example:
export DB_USER=sailline DB_PASSWORD=dev DB_NAME=sailline_app
export DB_HOST=127.0.0.1 DB_PORT=5432

# Apply the bootstrap once (extension + grants), as the superuser:
psql -h 127.0.0.1 -U postgres -d sailline_app -f ../infra/schema.sql

# Then run migrations as the app user:
alembic upgrade head
alembic current   # confirms HEAD revision
```

### Production (one-time stamp)

The production database already contains `user_profiles` and `race_sessions` from the pre-Alembic era. The baseline migration (`0001_baseline.py`) matches their current shape, so production needs to be **stamped** as already at that revision — not upgraded — to avoid `relation already exists` errors.

From Cloud Shell:

```bash
# Start the proxy in the background. cloud-sql-proxy is pre-installed in Cloud Shell.
cloud-sql-proxy sailline:us-central1:sailline-db &

# Pull the app password from Secret Manager.
export DB_USER=sailline DB_NAME=sailline_app
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=sailline-db-app-password)
export DB_HOST=127.0.0.1 DB_PORT=5432

# Apply the slimmed-down bootstrap (CREATE EXTENSION + GRANTs are
# idempotent; the new GRANT USAGE, CREATE on schema public is what
# lets the next step actually work).
gcloud sql connect sailline-db --user=postgres --database=sailline_app < infra/schema.sql

# Stamp the DB as already at revision 0001.
cd backend
alembic stamp 0001
alembic current   # → 0001 (head)

# Tear down the proxy.
kill %1
```

This is a one-time operation. Future migrations on prod use `upgrade`, not `stamp`.

---

## Creating a new migration

From `backend/`:

```bash
alembic revision -m "add_track_points"
```

This creates `migrations/versions/000N_add_track_points.py` with empty `upgrade()` and `downgrade()` functions. Fill them in with raw SQL via `op.execute(...)`. Pattern:

```python
def upgrade() -> None:
    op.execute("""
        CREATE TABLE track_points (
            id          BIGSERIAL PRIMARY KEY,
            session_id  UUID REFERENCES race_sessions ON DELETE CASCADE,
            recorded_at TIMESTAMPTZ NOT NULL,
            position    GEOGRAPHY(POINT, 4326) NOT NULL,
            speed_kts   FLOAT,
            heading_deg FLOAT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE INDEX track_session_time_idx
            ON track_points(session_id, recorded_at)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS track_session_time_idx")
    op.execute("DROP TABLE IF EXISTS track_points")
```

Use raw SQL rather than Alembic's `op.create_table()` helper. It keeps migrations readable and matches how the runtime app talks to Postgres (asyncpg, raw SQL, no ORM).

Bump the revision number sequentially: `0001`, `0002`, `0003`. The `down_revision` field tells Alembic which migration this one follows — set it to the prior revision's ID.

---

## Applying migrations

### Preview the SQL first

For anything non-trivial, generate the SQL without applying it:

```bash
alembic upgrade head --sql
```

Prints the exact statements that would run. Especially worth doing for `ALTER TABLE`, anything with data movement, or anything you've never run before.

### Apply against production

From Cloud Shell:

```bash
cloud-sql-proxy sailline:us-central1:sailline-db &
export DB_USER=sailline DB_NAME=sailline_app
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=sailline-db-app-password)
export DB_HOST=127.0.0.1 DB_PORT=5432

cd backend
alembic upgrade head
alembic current   # confirm new HEAD
kill %1           # stop the proxy
```

Then **flush the connection pool** on the running API so it picks up the new schema. Without this, asyncpg can keep stale prepared statements that reference old column names — exactly the failure mode that caused the third re-debug yesterday:

```bash
gcloud run services update sailline-api \
    --region=us-central1 \
    --update-env-vars=BUMP=$(date +%s)
```

The BUMP env var forces a new revision, which gives every container a fresh asyncpg pool.

### Rolling back

```bash
alembic downgrade -1
```

Only meaningful in dev. In production, prefer rolling forward with a new migration that reverses the change — that keeps the history honest and reviewable.

---

## Permissions note

Most migrations should run cleanly as the `sailline` app user. `infra/schema.sql` set `ALTER DEFAULT PRIVILEGES`, so any new table created by `sailline` is automatically usable by `sailline`. Migrations that need superuser (e.g. installing a new extension) should run as `postgres`:

```bash
export DB_USER=postgres
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=sailline-db-postgres-password)
alembic upgrade head
```

Then switch the env vars back to `sailline` for normal operation.

---

## Troubleshooting

**`alembic upgrade head` says "Can't locate revision identified by '0001'"**
You ran `alembic` from the wrong directory. It must be run from `backend/` where `alembic.ini` lives.

**`psycopg.OperationalError: connection failed`**
`cloud-sql-proxy` isn't running, or `DB_HOST`/`DB_PORT` are wrong. Check `ps aux | grep cloud-sql-proxy` and the proxy's logs.

**`relation "user_profiles" already exists` on production**
You ran `alembic upgrade` instead of `alembic stamp 0001` for the initial sync. Run `alembic stamp 0001` to mark the revision applied without re-running the DDL, then carry on.

**`permission denied for schema public` when running migrations as `sailline`**
The new `infra/schema.sql` grants `USAGE, CREATE on schema public` to `sailline`, but if your prod DB was bootstrapped with the old version this grant is missing. Re-apply the bootstrap as `postgres` (it's idempotent) and retry.

**API still returns "column does not exist" after migration**
The asyncpg pool is holding stale prepared statements from before the migration. Force a Cloud Run revision rollover with the BUMP env var trick shown in the apply section.

**`alembic current` returns nothing**
The DB has no `alembic_version` table yet — Alembic has never run against it. Either `alembic upgrade head` (fresh DB) or `alembic stamp 0001` (existing DB) to initialize it.
