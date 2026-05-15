# 2026-05-15 — Session D4: User profile (display_name, avatar, sailing & safety)

## What we worked on

Built out the user profile so crew rows can render real names instead
of raw Firebase UIDs, and so distance-race entries (Chicago Mac,
Bayview Mac, Bermuda, Transpac) can later draw on already-captured
crew credentials. Combined what was originally scoped as D4 + D5 into
a single session.

End state:

* `user_profiles` carries display_name, email, profile_complete,
  phone, bio, avatar_url, plus a sailing-and-safety bundle
  (weight_lb, emergency contact, World Sailing sailor ID/category,
  Safety-at-Sea cert expiry).
* New `GET/PATCH /api/users/me` echoes every field; PATCH uses
  `exclude_unset=True` so omitting a key leaves it alone.
* New `POST/DELETE /api/users/me/avatar` accept multipart, resize
  via Pillow to 256×256 WebP, upload to GCS at
  `https://storage.googleapis.com/{bucket}/{uid}.webp`.
* New `ProfileView.jsx` with two sections (Profile, Sailing & safety),
  forced-first-visit mode for email sign-ups missing a name.
* `BoatEditor` crew section now renders avatar + display_name → email
  → uid fallback chain.
* Menu drawer's Settings entry now opens ProfileView.

## Files changed

### Backend

* **NEW** `backend/migrations/versions/0015_add_user_profile_fields.py`
  — adds 13 columns to `user_profiles`; backfills
  `profile_complete = TRUE` for pre-existing rows so already-working
  accounts aren't yanked into ProfileView on next login.
* `backend/app/auth.py` — `_ensure_profile` UPSERT now writes
  `email`, `display_name`, `profile_complete` from Firebase token
  claims using `COALESCE` so user edits aren't clobbered. The OR on
  `profile_complete` is monotonic — once TRUE, stays TRUE.
* `backend/app/routers/users.py` — full rewrite. GET /me echoes the
  whole profile; PATCH /me uses dynamic SET clause built from
  `model_dump(exclude_unset=True)` so omitted keys aren't touched
  (this also fixes a latent pre-D4 bug where omitting
  `default_boat_id` silently cleared it). Display-name validation
  rejects empty/whitespace; weight bounded 50–500 lb; bio capped at
  1000 chars; world_sailing_category mirrored as a Pydantic Literal.
  POST/DELETE /me/avatar uses the new avatar service.
* `backend/app/routers/crew.py` — `list_crew` JOIN returns email,
  display_name, avatar_url. `update_member_role` RETURNING now joins
  the same fields so the patched row keeps its UI label. New
  `CrewMemberOut` fields are nullable so members whose profile pre-
  dates the D4 migration still surface (just without a name) until
  they next log in.
* **NEW** `backend/app/services/avatars.py` — pure-function
  `process_avatar` (Pillow validate + EXIF-fix + center-crop + 256
  WebP encode) and best-effort `store_avatar` / `delete_avatar`
  against GCS. Mirrors the cert upload pattern in `boats.py`.
* `backend/app/config.py` — adds `GCS_AVATARS_BUCKET` env var; null
  in dev means "skip the upload, return 503 from the endpoint".
* `backend/requirements.txt` — adds `Pillow==11.0.0`.

### Backend tests

* **NEW** `backend/tests/test_users_router.py` — GET/PATCH/avatar
  coverage. 14 tests including: full echo on GET, profile_complete
  flip on display_name set, validation rejections (empty name,
  out-of-range weight, oversize bio, bad category), partial-PATCH
  isolation (sending only `weight_lb` doesn't touch other columns),
  default_boat_id 404 on non-owner, avatar upload happy path / empty
  / bad MIME / no bucket / delete.
* **NEW** `backend/tests/test_avatar_service.py` — 6 Pillow-side
  tests: WebP output shape, RGBA flatten, empty/oversize/bad-MIME
  rejection, undecodable bytes.
* `backend/tests/test_crew_router.py` — updated
  `test_list_crew_returns_members` + `test_patch_role_to_viewer`
  fixture rows to include the new `email`, `display_name`,
  `avatar_url` columns and assert they appear in the response.

### Frontend

* **NEW** `frontend/src/ProfileView.jsx` — two-section form (Profile,
  Sailing & safety) with avatar upload widget. Forced-first-visit
  mode hides the back button and changes the headline copy.
* `frontend/src/AppView.jsx` — full rewrite. Adds `{ kind: "profile"
  }` view, force-routes when /me returns `profile_complete: false`
  (except during accept-invite). Settings nav now opens ProfileView.
  Menu drawer shows the avatar thumbnail next to the display label.
* `frontend/src/BoatEditor.jsx` — CrewSection renders avatar +
  display_name → email → uid fallback chain (monospace only when the
  uid fallback fires). New `CrewAvatar` component with initial-letter
  fallback.

### Verification

* `pytest tests/test_avatar_service.py` — **6/6 PASS** in the
  sandbox.
* `pytest tests/test_users_router.py tests/test_crew_router.py`
  — **not verified in sandbox** because the OneDrive/E: mount served
  stale versions of `users.py` and `auth.py` to the bash session.
  Run on Windows before pushing.

## Decisions made and rationale

1. **Combine D4 + D5 into one session.** Original plan split required
   ("display_name + email") and optional ("bio, picture, sailing
   creds"). User confirmed wanting it all done today, so we shipped a
   single migration and one ProfileView screen rather than building
   the credentials block twice.

2. **Existing-row backfill: `profile_complete = TRUE`.** Avoids
   yanking already-working users into a forced ProfileView on next
   login. New rows still default to FALSE — the auth UPSERT flips
   them when a `name` claim is present (Google), otherwise the user
   sees the forced view exactly once.

3. **`COALESCE(existing, EXCLUDED)` on UPSERT.** Auth-side backfills
   email/display_name from Firebase claims only when the DB column is
   currently NULL. User edits to display_name are not clobbered when
   the token claim differs. `profile_complete` is OR-ed (monotonic)
   so re-saves can never un-complete a profile.

4. **`model_dump(exclude_unset=True)` for PATCH.** Distinguishes
   omitted vs. explicit-null. Side effect: fixes a latent pre-D4 bug
   where a PATCH carrying only `default_boat_id` would silently clear
   any other field (Pydantic gave us a model full of None defaults).
   Flagged + approved.

5. **Avatar storage: deterministic blob name + public bucket.** One
   file per user (`{uid}.webp`) so each upload overwrites — no orphan
   GC, no signed URLs to refresh. Cache-bust via `?v={epoch}` query
   string on the URL we store. Public-read because avatars are
   intentionally social.

6. **Avatar is 256×256 WebP at q=85.** Same file serves both the
   ProfileView preview and the crew thumbnail. WebP gives roughly
   10× smaller payloads than JPEG. ImageOps.fit + LANCZOS for the
   square crop. EXIF-transpose first so portrait phone photos land
   right-side up.

7. **Sailing-and-safety fields all optional.** Distance-race
   pre-fill is the long-term value, but nothing blocks profile
   completion on them. Validation only enforces what's clearly
   wrong (weight 50–500 lb, bio ≤ 1000 chars, category in the
   ISAF Group 1/2/3 enum).

8. **Forced ProfileView exempts accept-invite.** A brand-new user
   redeeming an invite link should be able to join the boat before
   completing their profile. They'll see the forced view next time
   they open the app.

## Open items / next steps

### Pre-deploy manual followup — provision the GCS avatars bucket

Cloud Build won't auto-create this. Run **once**, then add
`GCS_AVATARS_BUCKET=sailline-avatars` to the Cloud Run env vars
(via `infra/cloudbuild.yaml` `--set-env-vars` or the console).

```powershell
# PowerShell — replace project ID if different
$PROJECT = "sailline"
$BUCKET  = "sailline-avatars"

gcloud storage buckets create gs://$BUCKET `
  --project=$PROJECT `
  --location=us-central1 `
  --uniform-bucket-level-access

# Public-read (avatars are intentionally public — same convention
# as other social apps; no signed URLs needed).
gcloud storage buckets add-iam-policy-binding gs://$BUCKET `
  --member=allUsers `
  --role=roles/storage.objectViewer

# Optional: lifecycle rule to auto-delete blobs for users who
# delete their account. Defer until we have an account-deletion
# flow — manual delete via DELETE /api/users/me/avatar is enough
# for now.
```

Then edit `infra/cloudbuild.yaml` to add `GCS_AVATARS_BUCKET=sailline-avatars`
to the deploy step's `--set-env-vars`. No CORS configuration needed
— frontend uses plain `<img src>` not crossorigin-fetch.

### Migration runbook

Migration 0015 is **additive** (ALTER ADD COLUMN with nullable
columns + one UPDATE backfill). Per `docs/migrations.md`, apply
before pushing:

```powershell
cd backend
alembic upgrade head
```

The backfill UPDATE on `user_profiles` touches every row but the
table is tiny (< 1k rows in prod) — sub-second locked write, safe to
run during traffic.

### Verification on Windows

The sandbox couldn't run the router tests this session because the
E: drive mount served stale versions of `users.py` and `auth.py` to
the bash session. Before pushing:

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
pytest tests/test_avatar_service.py tests/test_users_router.py tests/test_crew_router.py -v
```

And the frontend (sandbox bus-errors on vitest, also per memory):

```powershell
cd frontend
npm test
npm run build
```

### Infra debt addressed this session (bonus)

Discovered that the prod Cloud Run deploy was missing four env vars
since auto-deploy began — `--set-env-vars` was wiping them on every
push. Fixed in `infra/cloudbuild.yaml`:

* `GCS_CERTS_BUCKET=sailline-certs` — D2 cert PDFs were being parsed
  but `stored_url` always came back null. Now persisted.
* `GCS_AVATARS_BUCKET=sailline-avatars` — new in D4.
* `RACE_POSTPROCESS_JOB=projects/sailline/locations/us-central1/jobs/race-postprocess`
  — D1 postprocess trigger was silently no-oping. AI summary + wind
  snapshot start running on next finish.
* `ANTHROPIC_API_KEY` (Secret Manager: `anthropic-api-key:latest`)
  — D1 AI summaries were degrading to "summary unavailable" placeholder.
* `SENDGRID_API_KEY` (Secret Manager: `sendgrid-api-key:latest`)
  — D3 email invites were no-oping; owners had to share accept URLs
  manually.
* `EMAIL_FROM_ADDRESS=gray.vanderlinde@gmail.com` — single-sender
  verification path; the default `noreply@sailline.app` was a
  placeholder for an unowned domain.

IAM also granted: `secretmanager.secretAccessor` on both new secrets
to `sailline-api@sailline.iam.gserviceaccount.com`; `run.invoker` on
the `race-postprocess` job to the same SA.

### SendGrid trial expiry — followup scheduled

SendGrid trial runs out **2026-07-14**. After that the account drops
to the free tier (100 emails/day) — invite volume is well within that,
so paid plan not required.

The bigger issue: emails currently send From: `gray.vanderlinde@gmail.com`
which Gmail's DMARC policy doesn't authorize SendGrid to send under,
so deliverability is mediocre (many invite emails likely spam-foldered).

**Action by ~2026-07-07:**
1. Buy a domain (`sailline.app` if available; any TLD works).
2. SendGrid → Domain Authentication, add the DNS records at registrar.
3. Update `EMAIL_FROM_ADDRESS` in cloudbuild.yaml to `noreply@<domain>`.
4. Optionally point Firebase Hosting at the custom domain and update
   `APP_BASE_URL`.

Reminder scheduled for 2026-07-01 09:00 CT
(`sailline-sendgrid-domain-reminder`).

### Followup work (not blocking)

* **Race-entry pre-fill.** The whole point of the sailing-and-safety
  bundle is auto-populating Mac/Bermuda entry forms. Spec out the
  format conversion (our category enum → ISAF code, our cert expiry
  date → Mac form date format) in a future session.
* **Crew-completion warning on RaceEditor.** For distance-race
  modes, surface a banner if any crew is missing
  weight/emergency-contact/safety-cert. Belongs in the race entry
  flow, not the boat editor.
* **AcceptInviteView name capture.** When a new user redeems an
  invite, we could prompt for display_name inline so they don't
  have to do a second pass through ProfileView. Defer until we see
  whether this is actually friction.

## Technical debt flagged

* **Avatar URL cache-busting via epoch query string.** Works because
  GCS responds to query-string variants as the same object. If we
  ever move avatars behind a CDN that hashes the path, the busting
  has to move to the path. Note in the avatars module.
* **Pillow added to runtime deps.** ~5 MB of native code, only used
  on the avatar endpoint. Acceptable for now; if cold-start matters
  more, move avatar processing to a Cloud Run Job triggered on
  upload (would also unblock background re-processing if we ever
  change the output format).
* **`profile_complete` is a single bool, not a per-field flag.** Good
  enough for the v1 forced-view gate; if we later want gates on
  specific fields (e.g. "weight required for distance races"), we'd
  derive at read time rather than storing more flags.
