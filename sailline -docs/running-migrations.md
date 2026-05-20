# Running Alembic migrations against Cloud SQL

A 5-minute happy path. Skip to **Per-migration sequence** if everything is
already installed.

---

## One-time setup

Done once per dev machine.

```powershell
# Install local-only DB driver (asyncpg is what production uses; psycopg is for Alembic)
python -m pip install "psycopg[binary]"

# Download Cloud SQL Auth Proxy to the repo root
cd E:\Personal\Coding\SailLine
Invoke-WebRequest `
  -Uri "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v2.21.3/cloud-sql-proxy.x64.exe" `
  -OutFile "cloud-sql-proxy.exe"

# Authenticate gcloud + Application Default Credentials
gcloud auth login
gcloud auth application-default login
```

Confirm `cloud-sql-proxy.exe` is gitignored (it's a 20MB binary).

---

## Per-migration sequence

### Window 1 — Cloud SQL Auth Proxy

Leave this running for the whole session.

```powershell
cd E:\Personal\Coding\SailLine
.\cloud-sql-proxy.exe sailline:us-central1:sailline-db --port 5432
```

Wait for `The proxy has started successfully and is ready for new connections!`

If port 5432 is already in use (typically a stale proxy from a previous
session that didn't clean up), use `--port 5433` and set `DB_PORT=5433` in
window 2.

### Window 2 — Env vars + Alembic

```powershell
cd E:\Personal\Coding\SailLine\backend

$env:DB_USER="sailline"
$env:DB_NAME="sailline_app"
$env:DB_HOST="127.0.0.1"
$env:DB_PORT="5432"
$env:DB_PASSWORD = (gcloud secrets versions access latest --secret=sailline-db-postgres-password).Trim()

# Sanity check before running anything
Write-Host "Password length: $($env:DB_PASSWORD.Length)"   # should be 24
python -c "import os, psycopg; psycopg.connect(host=os.environ['DB_HOST'], port=int(os.environ['DB_PORT']), dbname=os.environ['DB_NAME'], user=os.environ['DB_USER'], password=os.environ['DB_PASSWORD']).close(); print('OK')"

# Run the migration
python -m alembic upgrade head

# Verify
python -m alembic current
```

---

## Critical gotcha — the password secret name

**The two secrets have drifted, and which one works has FLIPPED at least
once.** Do not trust this section to name the live one — verify
empirically each session (see below).

As of **2026-05-19**, the **working** password for the `sailline` DB
user is in:

```
sailline-db-app-password        ✅ currently accepted by the database
sailline-db-postgres-password   ❌ currently rejected (was the working one before 2026-05-19)
```

Before 2026-05-14 it was the reverse. Neither secret reliably tracks
the database's actual `sailline` password — the rotation pipeline is
broken and has been re-broken in both directions. Production runs on a
long-lived Cloud Run instance with a cached value, so it keeps working
even when both secrets are wrong.

**Verify which secret works before migrating** (cheap, no migration):

```powershell
foreach ($s in "sailline-db-app-password","sailline-db-postgres-password") {
  $p = (gcloud secrets versions access latest --secret=$s).Trim()
  try {
    python -c "import psycopg; psycopg.connect(host='127.0.0.1', port=5432, dbname='sailline_app', user='sailline', password='$p').close()"
    Write-Host "$s WORKS" -ForegroundColor Green
  } catch {
    Write-Host "$s rejected" -ForegroundColor Red
  }
}
```

Set `$env:DB_PASSWORD` from whichever prints WORKS, then run Alembic.

**Real fix (tracked separately):** reconcile the `sailline` role
password with a single source of truth and repair the rotation
pipeline. Until then this is empirical-verification-every-time.

---

## Common error → fix

| Error | What it means | Fix |
|---|---|---|
| `Fatal error in launcher … alembic.exe` | Bare `alembic` resolves to a stale wrapper | Use `python -m alembic` instead |
| `No 'script_location' key found` | Wrong working directory | `cd backend` first |
| `missing required env var 'DB_USER'` | Env vars vanished (new window, or old session) | Re-run the env var block in Window 2 |
| `connection timeout expired` | Proxy not running, or wrong DB_HOST | Check Window 1, verify `Get-NetTCPConnection -LocalPort 5432 -State Listen` |
| `password authentication failed` | Wrong secret name, or password mangled by PowerShell | Use the `postgres-password` secret; pull with `.Trim()` |
| `bind: Only one usage of each socket address` | Old proxy still running on the port | Find + kill: `Get-NetTCPConnection -LocalPort 5432 -State Listen \| Select OwningProcess`, then `Stop-Process -Id <id> -Force`. Or use `--port 5433`. |
| `ModuleNotFoundError: psycopg` | Driver not installed locally | `python -m pip install "psycopg[binary]"` |

---

## Optional: bootstrap script

Drop the env-var block into `backend/scripts/setup-migration-env.ps1` (and
gitignore the directory). Then in Window 2:

```powershell
cd E:\Personal\Coding\SailLine\backend
. .\scripts\setup-migration-env.ps1
python -m alembic upgrade head
```

---

## Post-migration

After a migration that changes types or columns referenced by the running
API, redeploy Cloud Run so it picks up the new schema:

```powershell
gcloud run services update sailline-api `
  --region=us-central1 `
  --update-env-vars DEPLOY_ID=$(Get-Date -Format "yyyyMMddHHmm")
```

Tail logs to confirm the new revision starts cleanly:

```powershell
gcloud run services logs read sailline-api --region=us-central1 --limit=50
```
