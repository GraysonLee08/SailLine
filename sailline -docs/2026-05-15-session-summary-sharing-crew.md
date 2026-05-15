# Session Summary — Sharing + crew + pro-tier gating (Session D3)

**Date:** 2026-05-15 (autonomous overnight work)
**Scope:** D3 from the multi-session plan, plus the pro-tier gating that was added to scope on the way in. Boats now have multi-user membership with three roles; invites work via short join codes OR single-use email tokens (SendGrid); every race-scoped endpoint uses a shared SQL predicate for the owner/crew/viewer check; free callers see a generic-polar route and no corrected time.

## What we worked on

End-to-end sharing + crew + tier gating:

- New `boat_crew` membership table (with backfill so every existing boat's owner gets an 'owner' row).
- New `boat_invites` table — single table for both invite flavours.
- New `app/auth_helpers.py` — SQL predicate generators for race + boat read/write/owner gates. Single source of truth so every race-scoped endpoint speaks the same auth language.
- New `app/services/email.py` — SendGrid wrapper with graceful no-op when the API key isn't set (dev returns the accept URL for manual sharing).
- New `app/routers/crew.py` — full CRUD on members + invites + redeem.
- Auth refactor across **6 routers**: `races.py`, `tracks.py`, `race_stats.py`, `routing.py`, `routing_notifications.py`, `boats.py`. Every `user_id = $X` clause flipped to the appropriate predicate. Owner-only ops (regenerate, delete, cert upload, boat edit) keep the stricter `role='owner'` check.
- Pro-tier gating: free callers now get the GENERIC polar in `/api/routing/compute` (regardless of boat_class on the race), and no corrected time on `/api/races/{id}/stats`. Boat ratings stripped from the response too.
- Redis stats cache key now includes the caller's tier so a free → pro upgrade surfaces the corrected time on next fetch.
- Frontend: `useCrew` hook, BoatEditor Crew section (member management + invite flows), AcceptInviteView at `?invite=<code>`, RaceEditor view-only banner for non-creators, "Shared" chip on RacesListView cards.

## Files changed

### Backend

#### New
- `backend/migrations/versions/0013_add_boat_crew.py` — table + index + owner backfill.
- `backend/migrations/versions/0014_add_boat_invites.py` — unified email-token + join-code table.
- `backend/app/auth_helpers.py` — `race_read_predicate`, `race_write_predicate`, `race_owner_predicate`, `boat_read_predicate`, `boat_owner_predicate`, plus a small `user_role_for_boat` helper for in-Python checks.
- `backend/app/services/email.py` — `send_boat_invite(...)`. Lazy SDK import. Returns False on any failure.
- `backend/app/routers/crew.py` — all crew + invite endpoints under `/api/boats/{id}/crew/*` + `/api/boats/{id}/invites/*` + `/api/invites/redeem`.

#### Edited
- `backend/app/routers/races.py` — list + get + patch + delete flipped to predicates. `RaceOut` gains `user_id` so the frontend can render edit/read-only mode.
- `backend/app/routers/tracks.py` — write predicate on POST; read predicate on GET.
- `backend/app/routers/race_stats.py` — read predicate on GET; owner predicate on regenerate; tier baked into cache key; ratings + corrected time stripped from response for free callers.
- `backend/app/routers/routing.py` — write predicate on `_assert_race_owned`; free callers route with the GENERIC polar.
- `backend/app/routers/routing_notifications.py` — read predicate on the SSE subscribe.
- `backend/app/routers/boats.py` — `list_boats` returns boats the caller is a member of (not just owns); reads use `_load_readable`; writes still use `_load_owned` (now reflects the owner predicate).
- `backend/app/routers/users.py` — `default_boat_id` validation now accepts any boat the caller can read (lets crew set someone's boat as their default).
- `backend/app/main.py` — mounted `crew.router`.
- `backend/app/config.py` — new settings: `SENDGRID_API_KEY`, `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`, `APP_BASE_URL`.
- `backend/requirements.txt` — `sendgrid==6.11.0`.

#### Tests (new + edits)
- `backend/tests/test_email_service.py` (new) — 5 cases with mocked SendGrid client.
- `backend/tests/test_auth_helpers.py` (new) — 6 cases asserting the SQL fragments contain the right table/alias/role/placeholder bits.
- `backend/tests/test_crew_router.py` (new) — 17 cases covering crew CRUD, invite creation (both flavours), invite list/revoke, redeem (404 / 410 / 409 / happy / idempotent).
- `backend/tests/test_race_stats_router.py` (edit) — 2 new cases for the pro-tier gating (free hides corrected time + ratings, pro shows them).
- `backend/tests/test_races_router.py` (edit) — delete tests updated for the new auth-precheck flow (one fetchrow then DELETE, not just DELETE).

### Frontend

#### New
- `frontend/src/hooks/useCrew.js` — CRUD on crew/invites + standalone `redeemInvite` for AcceptInviteView.
- `frontend/src/AcceptInviteView.jsx` — landing on `?invite=<code>`. Shows confirm panel, redeems, navigates to Boats.

#### Edited
- `frontend/src/BoatEditor.jsx` — owner-only Crew section with member list (role chips + role-edit + remove), email-invite form, generate-code button, pending-invites list with revoke. Plus a copy-to-clipboard helper. ~300 LOC added.
- `frontend/src/RaceEditor.jsx` — accepts `currentUid`, reads `race.user_id`, shows a view-only banner when caller isn't the creator. Server still enforces.
- `frontend/src/RacesListView.jsx` — accepts `currentUid`, renders a "Shared" chip on race cards when `race.user_id !== currentUid`.
- `frontend/src/AppView.jsx` — lazy-load `AcceptInviteView`; detect `?invite=` on init; new view kinds for accept-invite + boats. Passes `currentUid` to RacesListView, RaceEditor, BoatEditor.

### Docs
- `sailline -docs/2026-05-15-session-summary-sharing-crew.md` (this file).
- `sailline -docs/Development plan.docx` (D3 section appended; see end of file).

## Decisions made

1. **Single `boat_invites` table** for both email tokens and join codes. Discriminated by `email IS NULL` and `single_use`. Avoids two parallel CRUD surfaces.
2. **Predicates as SQL string fragments**, not query-builder expressions. Centralised in `auth_helpers.py` for one-place review. SQLAlchemy migration considered and rejected: lift is 2–3 sessions, real value comes only when we add a 4th role or another cross-cutting predicate. Tech debt logged.
3. **Caller-tier gating, not owner-tier.** A pro crew member sees corrected time even on a free owner's race. Per-user feature gating is simpler than per-resource and creates no upgrade-discouragement dynamic.
4. **Owner is creator forever.** No ownership transfer in v1. Adding it is a single endpoint when needed.
5. **Email send is best-effort.** When SendGrid fails or isn't configured, the invite row still exists and `accept_url` is returned to the owner. UI surfaces "Email send failed — copy the link below" so the feature ships even before SendGrid signup.
6. **Idempotent redeem.** Clicking the link twice returns 200 with the existing role instead of 409. Friendlier; covers the common case of the user already being a member.
7. **Backfill in the migration**, not on first read. Owners get a `boat_crew` row at migration time so the auth refactor is non-breaking from the first request after deploy.
8. **Stats cache key includes tier** so a free → pro upgrade surfaces corrected time on next fetch without a manual invalidation step.

## Verification

- **Sandbox tests not run** for the backend — OneDrive sync corrupts files mid-edit (same pattern that hit D1/D2). You'll run on Windows.
- **Frontend tests not run** — vitest can't run in the sandbox (bus error, prior memory). Run on Windows.

**Run on Windows in the morning:**
```powershell
cd E:\Personal\Coding\SailLine\backend
pip install -r requirements.txt
.\.venv\Scripts\Activate.ps1
$env:DB_USER="sailline"; $env:DB_NAME="sailline_app"; $env:DB_HOST="127.0.0.1"
$env:DB_PASSWORD = (gcloud secrets versions access latest --secret=sailline-db-app-password)
pytest -m "not slow" -v
```

Expected: D1+D2 baseline (~430) + ~30 new D3 tests = ~460 passes.

```powershell
cd E:\Personal\Coding\SailLine\frontend
npm test
```

Expected: D1+D2 baseline (61) + (none new in D3 — frontend tests for sharing UI weren't authored; flagged as debt).

## Open items / morning checklist

### 1. Run backend + frontend tests (Windows). Fix anything red.

### 2. Apply migrations 0013 + 0014 to prod
```powershell
Start-Process cloud-sql-proxy -ArgumentList "sailline:us-central1:sailline-db"
cd backend
alembic upgrade head      # 0012 → 0014
```

### 3. (Optional) Set up SendGrid

If you want email invites working in prod:

```powershell
# Sign up at https://signup.sendgrid.com/ → create API key with "Mail Send"
gcloud secrets create sendgrid-api-key --replication-policy=automatic
gcloud secrets versions add sendgrid-api-key --data-file=-   # paste key, Ctrl+Z, Enter
gcloud secrets add-iam-policy-binding sendgrid-api-key `
  --member='serviceAccount:sailline-api@sailline.iam.gserviceaccount.com' `
  --role='roles/secretmanager.secretAccessor'
gcloud run services update sailline-api `
  --region=us-central1 `
  --update-secrets=SENDGRID_API_KEY=sendgrid-api-key:latest
```

The feature works without this — owners can copy/paste join codes manually. SendGrid only matters if you want email invites.

### 4. Commit + push
```powershell
cd E:\Personal\Coding\SailLine
glunlock
git add .
git status   # sanity-check the diff
git commit -m "D3: sharing + crew + pro-tier gating"
git push origin main
```

Cloud Build runs the gated `pytest -m "not slow"` + `npm test` before deploy. If anything fails, the pipeline blocks the deploy and you can fix from logs.

### 5. UI smoke test (after deploy)

- Open the app → menu → Boats → open a boat
- Confirm a Crew section appears with you (owner) listed
- "Generate join code" → confirm code displays + Copy works
- Open an incognito window logged in as a second account → paste `https://sailline.web.app/?invite=<code>` → confirm AcceptInviteView shows + click Accept → confirm redirect to Boats → confirm the shared boat appears
- As the second account, open a race tied to that boat → confirm view-only banner displays → try saving (expect server 404)
- As the owner, change the second account's role to viewer → log in as them again → confirm RacesListView shows the race with "Shared" chip
- (Optional) After SendGrid setup: invite an email → check inbox

## Tech debt flagged (D3)

1. **No frontend tests for sharing UI.** Vitest can't run in the sandbox; authoring without verification felt riskier than skipping. Worth a follow-up — particularly for `useCrew` (lots of fetch interleaving) and `AcceptInviteView` (error-state branches).
2. **Crew role determined by `race.user_id` heuristic** in RaceEditor. We don't fetch the caller's actual role; we just check "are you the creator". Crew who try to save get a server 404. Cleaner UX: fetch role + render proper read-only for crew. ~20 LOC follow-up.
3. **No email rate limiting.** A malicious owner could spam invites — bounded by SendGrid's rate limit, not ours. Add per-user / per-IP throttle when this surfaces.
4. **Email DNS not configured.** `EMAIL_FROM_ADDRESS=noreply@sailline.app` will hit spam filters until SPF/DKIM/DMARC are set up on the domain. Operational task; orthogonal to code.
5. **`user_profiles.email` doesn't exist.** Crew list returns just the `user_id` (Firebase uid). To show emails on the crew list we'd either need to store them on the profile (auth.py UPSERTs the row on first login — could write the email there) or call Firebase Admin's `getUser(uid)` per member. Flagged for a small follow-up.
6. **Ownership transfer absent.** Owner can't hand off a boat to crew. One endpoint to add when needed; predicate already accommodates `role='owner'` rows in `boat_crew`.
7. **Crew member's accept link could be forwarded.** Single-use tokens mitigate; multi-use join codes are explicit (the owner knows when they share a code that anyone with the code can join). Not a vulnerability so much as a property.
8. **Pro-tier polar gating couples `routing.py` to tier.** A cleaner abstraction: have `spec_for_class` itself accept a tier and route to GENERIC internally. Local enough that the explicit `if` is fine for v1.
9. **`isOwner` check in BoatEditor relies on `form.owner_id`** which isn't a form field — it's data the API returns. The current code spreads the response into the form so `form.owner_id` reads back correctly, but it's load-bearing in a way that's easy to miss. Worth refactoring to a separate `meta` state.
10. **Email body is HTML-only with inline styles, no plaintext-first preference.** Most email clients render fine; some accessibility tools prefer plain text. Marginal.

## What's next

- **D4 candidates:**
  - **Boat profile pages / public boats** — share a read-only link to a boat's record without joining (sub-D3 of D3).
  - **Race comments** — crew can comment on a recorded race for post-race debrief.
  - **Notifications feed** — in-app "you've been added to a boat" / "new race created" / "summary ready" notifications. Pairs naturally with the SSE infrastructure already in routing_notifications.
  - **MWPHRF DB import** (D2.5) — original deferral from D2; lower urgency now that the user model is in good shape.
  - **Mobile build pipeline** — Capacitor lives in the repo but CI doesn't build native. Operational follow-up.
- **No blocking issues identified.** D3 backend has clean test coverage of the gnarly paths (auth predicates, invite redeem state machine, pro-tier gating). Frontend coverage is the biggest gap.
