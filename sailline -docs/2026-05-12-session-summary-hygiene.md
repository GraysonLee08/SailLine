# 2026-05-12 — Hygiene side-quests

## What we worked on

Knocked out a batch of low-risk repo hygiene improvements: line-ending normalization, CI test gating on both pipelines, and editor/Node version pinning.

## Files changed

- `E:\Personal\Coding\SailLine\.gitattributes` (new) — `* text=auto eol=lf` plus binary carve-outs (PNG/JPG/PDF/DOCX/GRIB2 etc.). Stops CRLF/LF churn in the repo.
- `E:\Personal\Coding\SailLine\.editorconfig` (new) — UTF-8, LF, final-newline, trim-trailing-whitespace, 2-space default, 4-space for Python, trailing-whitespace preserved in Markdown.
- `E:\Personal\Coding\SailLine\.nvmrc` (new) — `20`, matches the `node:20` builder image in both cloudbuild files.
- `E:\Personal\Coding\SailLine\infra\cloudbuild.frontend.yaml` — added `test` step (`npm test` / vitest) between `install` and `env`. Failing test blocks deploy.
- `E:\Personal\Coding\SailLine\infra\cloudbuild.yaml` — added `test` step at the top of the pipeline (`pytest -m "not slow"` in a `python:3.12-slim` container). Failing test blocks docker build/push/deploy.
- `E:\Personal\Coding\SailLine\CLAUDE.md` — Deploy section now states both pipelines gate on tests; frontend "no test script configured" line replaced with the truth (vitest is wired and gated).

## Decisions and rationale

- **Skip `libeccodes-dev` apt-install in the backend CI test step.** `cfgrib` is only imported lazily inside functions in `main.py` and `health.py`, not at module-load time, so pytest collection won't trip on it. Saves ~10s of apt-install per build.
- **`-m "not slow"` for backend CI.** Matches the "default for CI-style runs" guidance already in `CLAUDE.md`; keeps live NOAA fetches out of the deploy path.
- **Backend test step uses `python:3.12-slim`**, not a multi-stage Dockerfile refactor. The Dockerfile comment explicitly calls multi-stage "overkill" for this app — respected that.
- **`.nvmrc` placed at repo root**, not in `frontend/`. nvm/fnm walk upward from cwd, so it works whether you `cd frontend` or stay at root.
- **`.editorconfig` keeps trailing whitespace in `*.md`** because Markdown uses two trailing spaces as a hard line break.

## Open items / next steps (parked, user said hold)

- `.env.example` files for backend + frontend (neither exists today).
- Pin Docker base images to `@sha256:...` digests in both cloudbuild files for reproducible/supply-chain-safe builds.
- Add `ruff` to backend + a CI lint gate. First run will reformat files, so it deserves its own commit.
- Dependabot or Renovate config for automated npm/pip update PRs.
- `"engines": { "node": ">=20 <21" }` in `frontend/package.json` to back up `.nvmrc` with an npm-side warning.

## Tech debt flagged

- **Backend CI test step pip-installs all of `requirements.txt` from scratch each build** (~60-90s added per deploy). A `requirements-test.txt` split or a Cloud Build wheel cache (`--cache-from`) would shave most of that. Worth doing if backend deploy latency starts to feel painful.

## Manual follow-up (PowerShell)

After committing `.gitattributes`, flush existing CRLF noise from the index:

```powershell
git add .gitattributes
git commit -m "Add .gitattributes for LF normalization"
git add --renormalize .
git commit -m "Renormalize line endings"
```

The renormalize commit will look enormous in the diff but is byte-only — no semantic changes.
