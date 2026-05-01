# Schema Migrations

SailLine uses [Alembic](https://alembic.sqlalchemy.org/) to manage Postgres schema changes. Every change to a table — new column, new index, new table — goes through a migration file in `backend/migrations/versions/` so we have a versioned, reviewable history that applies the same way every time.

`infra/schema.sql` only handles one-time bootstrap (PostGIS extension, role grants). Don't add tables there.

---

## Why Alembic (and what it fixes)

The pre-Alembic workflow used `CREATE TABLE IF NOT EXISTS` in `infra/schema.sql`. That makes re-runs idempotent but blind to schema drift — if a table already exists with a different shape, the script silently no-ops. We hit this exact failure on 2026-04-30 (see `docs/2026-04-30-session-summary.md`): the script reported `CREATE TABLE` and exited cleanly while the table on disk still had the old columns. Three re-applies before we caught it.

Alembic tracks applied revisions in a dedicated `alembic_version` table. Migrations fail loudly when something is wrong, and a real DB connection means the migration runner sees the actual current state instead of guessing.

---

## File layout

```
backend/
├── alembic.ini                    # config: script location, file template, logging
└── migrations/
    ├── env.py                     # builds DB URL from env vars; runs migrations
    ├── script.py.mako             # template for new migration files
    └── versions/
        ├── 0001_baseline.py       # captures user_profiles + race_sessions
        └── 0002_add_track_points.py
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
alembic current
```

### Production (one-time stamp)

The production database already contains `user_profiles` and `race_sessions` from the pre-Alembic era. The baseline migration (`0001_baseline.py`) matches their current shape, so production needs to be **stamped** as already at that revision — not upgraded — to avoid `relation already exists` errors.

From Cloud Shell:

```bash
# Reusable venv for the Alembic CLI (~/sailline-venv persists between sessions)
python3 -m venv ~/sailline-venv
source ~/sailline-venv/bin/activate
pip install alembic==1.13.3 'psycopg[binary]==3.2.3'

# Forward the private-IP Cloud SQL instance to localhost
cloud-sql-proxy sailline:us-central1:sailline-db &

# Apply the bootstrap as superuser
export PGPASSWORD=$(gcloud secrets versions access latest --secret=sailline-db-postgres-password)
psql -h 127.0.0.1 -U postgres -d sailline_app -f infra/schema.sql
unset PGPASSWORD

# Stamp the DB at 0001 as the app user
export DB_USER=sailline DB_NAME=sailline_app
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=sailline-db-app-password)
export DB_HOST=127.0.0.1 DB_PORT=5432
cd backend
alembic stamp 0001
alembic current   # → 0001 (head)

# Tear down
unset DB_USER DB_PASSWORD DB_NAME DB_HOST DB_PORT
deactivate
kill %1
cd ~
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
            ...
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS track_points")
```

Use raw SQL rather than Alembic's `op.create_table()` helper. It keeps migrations readable and matches how the runtime app talks to Postgres (asyncpg, raw SQL, no ORM).

Bump the revision number sequentially: `0001`, `0002`, `0003`. The `down_revision` field tells Alembic which migration this one follows — set it to the prior revision's ID.

---

## Applying migrations

### Don't bother with `--sql` for preview

Alembic's `upgrade head --sql` is offline mode — it generates SQL without connecting to the DB, which means it can't see the `alembic_version` table and assumes the DB is empty. The output regenerates *every* migration from scratch, not just the unapplied ones. Useless for our setup.

If you want to confirm what will run before running it, use:

```bash
alembic current     # what revision is the DB actually at?
alembic history     # all migrations and their order
alembic heads       # latest revision in the codebase
```

The gap between `current` and `heads` is exactly what `upgrade` will apply.

### Apply against production

From Cloud Shell:

```bash
source ~/sailline-venv/bin/activate
cloud-sql-proxy sailline:us-central1:sailline-db &

export DB_USER=sailline DB_NAME=sailline_app
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=sailline-db-app-password)
export DB_HOST=127.0.0.1 DB_PORT=5432

cd backend
alembic upgrade head
alembic current
```

Then **flush the connection pool** on the running API so it picks up the new schema. Without this, asyncpg can keep stale prepared statements that reference old column names — exactly the failure mode that caused the third re-debug on 2026-04-30:

```bash
gcloud run services update sailline-api \
    --region=us-central1 \
    --update-env-vars=BUMP=$(date +%s)
```

The BUMP env var forces a new revision, which gives every container a fresh asyncpg pool.

Cleanup:

```bash
unset DB_USER DB_PASSWORD DB_NAME DB_HOST DB_PORT
deactivate
kill %1
cd ~
```

### Rolling back

```bash
alembic downgrade -1
```

Only meaningful in dev. In production, prefer rolling forward with a new migration that reverses the change — that keeps the history honest and reviewable.

---

## Permissions

The `sailline` app user runs migrations. `infra/schema.sql` grants it everything it needs in the typical case:

- `SELECT, INSERT, UPDATE, DELETE, REFERENCES` on existing tables (catch-up grants for `user_profiles` and `race_sessions`)
- `ALTER DEFAULT PRIVILEGES` so new tables created by Alembic auto-grant the same set
- `USAGE, CREATE` on schema `public` so Alembic can create `alembic_version` and new tables

Tables created by Alembic going forward are **owned by sailline**, not postgres. Owners have all privileges implicitly, so no follow-up GRANT is ever needed for FKs between Alembic-created tables.

The only situation requiring superuser is something the app user inherently can't do — installing a new extension, creating a new role, etc:

```bash
export DB_USER=postgres
export DB_PASSWORD=$(gcloud secrets versions access latest --secret=sailline-db-postgres-password)
alembic upgrade head
```

Then switch back to `sailline` for normal operation.

---

## Troubleshooting

**`alembic upgrade head` says "Can't locate revision identified by '0001'"**
You ran `alembic` from the wrong directory. It must be run from `backend/` where `alembic.ini` lives.

**`psycopg.OperationalError: connection failed`**
`cloud-sql-proxy` isn't running, or `DB_HOST`/`DB_PORT` are wrong. Check `ps aux | grep cloud-sql-proxy` and the proxy's logs.

**`relation "user_profiles" already exists` on production**
You ran `alembic upgrade` instead of `alembic stamp 0001` for the initial sync. Run `alembic stamp 0001` to mark the revision applied without re-running the DDL, then carry on.

**`permission denied for table <pre-Alembic table>` while running a migration**
The migration is creating a FK pointing at one of the original tables (`user_profiles` or `race_sessions`) and the catch-up grants in `infra/schema.sql` weren't applied. Re-apply `infra/schema.sql` as `postgres` — the `GRANT ... REFERENCES` block is idempotent. If for some reason the catch-up GRANT fails, run it manually:

```sql
GRANT REFERENCES ON user_profiles, race_sessions TO sailline;
```

Don't use `GRANT ... ON ALL TABLES IN SCHEMA public` — that includes `alembic_version` (owned by sailline, not postgres) and the whole statement aborts.

**`permission denied for schema public` when running migrations as `sailline`**
The new `infra/schema.sql` grants `USAGE, CREATE on schema public` to `sailline`, but if your prod DB was bootstrapped with the old version this grant is missing. Re-apply the bootstrap as `postgres` (it's idempotent) and retry.

**API still returns "column does not exist" after migration**
The asyncpg pool is holding stale prepared statements from before the migration. Force a Cloud Run revision rollover with the BUMP env var trick shown in the apply section.

**`alembic current` returns nothing**
The DB has no `alembic_version` table yet — Alembic has never run against it. Either `alembic upgrade head` (fresh DB) or `alembic stamp 0001` (existing DB) to initialize it.
