# Overnight work plan — 2026-05-25 → 2026-05-26

**Author:** Claude (drafted for Grayson's review before bed)
**Constraint:** no migrations, no `git push`, no Cloud Build triggers, no Windows-only steps. Everything below either lands as committed code in working files or as fresh `.md` deliverables. Anything that needs your machine is queued for the morning runbook at the bottom.

---

## Current state (3-line orient)

- `main` is clean except two uncommitted edits to today's session doc and `Development plan.docx` (close-out edits from Phase 1).
- Phase 1 (background GPS) shipped today; **Phase 2 (mobile management parity)** is the next phase per `2026-05-24_mobile-development-plan.md` but is unstarted.
- Last session flagged two follow-ups I can chip away at without you: avatar-service has no unit tests, job_trigger has no unit tests, and the Phase 2 plan itself hasn't been drafted.

---

## What I'll work on (in priority order)

### 1. Draft the Phase 2 plan — mobile management parity (HIGHEST VALUE)

**Deliverable:** `sailline -docs/2026-05-26_phase2-mobile-management-plan.md`

A full plan in the same shape as `2026-05-25_phase1-background-gps-plan.md` so you can wake up, read it over coffee, and approve/edit it. Phase 2 per the dev plan covers: boats CRUD, crew CRUD, race creation, all on the phone, against the existing REST endpoints.

Plan will cover:
- Screens: AuthGate → Home → BoatsList → BoatEdit → CrewList → CrewInvite → RacesList → RaceEditor (mobile equivalents).
- Navigation choice: `expo-router` (file-based, Expo-blessed) vs `@react-navigation/native` (more control, more wiring). **Recommendation will be expo-router** but I'll lay out the tradeoff for your call.
- Shared-package extraction strategy: which `frontend/src/hooks/use*.js` modules (`useBoats`, `useCrew`, `useRaces`) can move into `@sailline/shared` as framework-agnostic data hooks vs which stay web-only because they touch DOM/Mapbox.
- API client: extend `mobile/src/api.ts` with typed wrappers for the boats/crew/races endpoints, or import them from a new `packages/shared/src/api/` module shared with the web. **Recommendation: shared module** — eliminates the URL drift risk.
- Form UX: native form patterns vs porting the web JSX. **Recommendation: native** — web forms assume a keyboard+mouse + Mapbox click-to-set-mark interactions that don't translate.
- Test strategy: contract tests for the shared API client (mocked `fetch`) + a small set of RN component tests with `@testing-library/react-native`.
- Risk: native auth-token plumbing (Firebase) is already proven in Phase 1, so this is mostly UI + data binding.
- Sequencing within Phase 2: shared API client → boats screens → crew screens → race-create. Each is a reviewable PR-sized chunk.

I will **not** write any Phase 2 implementation code overnight. Plan only.

---

### 2. Unit tests for `app/services/avatars.py` (PURE FUNCTION, ZERO BLAST RADIUS)

**Deliverable:** `backend/tests/test_avatars_service.py`

Wait — `test_avatar_service.py` already exists; let me check overlap before drafting. If it covers `process_avatar` (the pure resize/validate function), I'll back off. If it only covers `store_avatar` (the GCS upload side), I'll add coverage for `process_avatar`:

- Rejects files > `MAX_UPLOAD_BYTES`.
- Rejects non-image content.
- Resizes a square JPEG to exactly 256×256 WebP.
- Resizes a wide JPEG to 256×256 WebP (verifies aspect-handling behavior matches the actual implementation — I'll read the code before asserting which behavior is "correct": center-crop vs letterbox).
- Round-trips: result is valid WebP and decodes back to a 256×256 image.

If the existing test already covers this, I'll instead audit the test for gaps and produce a `.md` note (no code change).

**Why safe overnight:** Pillow-only, no DB, no network, no GCP. Worst case: a test fails on the CI box for a reason I missed in the sandbox and you skip it in the morning.

---

### 3. Unit tests for `app/services/job_trigger.py` (HTTP-MOCKED, ZERO BLAST RADIUS)

**Deliverable:** `backend/tests/test_job_trigger.py`

The module is 113 lines, all `httpx` + Google auth, designed to be tolerant of every failure. Easy to mock cleanly. Tests will cover:

- Returns `None` / no-ops when `RACE_POSTPROCESS_JOB` env var is unset.
- Returns `None` / no-ops when `google.auth` import fails (simulate via monkeypatch).
- Returns `None` / no-ops when ADC lookup raises.
- Builds the correct Cloud Run v2 Admin API URL from settings.
- Sends the race_id as a `containerOverrides.args` entry.
- Logs (doesn't raise) on HTTP failure.

Same "respx" or `httpx.MockTransport` pattern other tests already use — I'll match the existing test style before writing.

---

### 4. Audit shared-package mirror drift (READ-ONLY, OUTPUTS A DOC)

**Deliverable:** `sailline -docs/2026-05-26_shared-mirror-audit.md`

`backend/app/regions.py` and `frontend/src/lib/regions.js` (now `packages/shared/src/regions.js`) are documented as a "single source of truth, edit both together." I'll diff them line by line and produce a short report:

- Are the region names identical?
- Are the bboxes identical?
- Is anything in one that's missing in the other?
- Does the `packages/shared/src/regions.js` actually match `frontend/src/lib/regions.js` (recent commit `40cd482` says frontend cutover is complete — verify).

Same audit for `morfMarks`, `morfCourses`, `boatClasses` (Python equivalents may or may not exist; the audit will say).

Pure read; produces a doc, changes no code.

---

### 5. Frontend hook test coverage (NEW TEST FILES ONLY, NO PRODUCTION CHANGES)

**Deliverables:**
- `frontend/src/hooks/useRaces.test.js`
- `frontend/src/hooks/useBoats.test.js`
- `frontend/src/hooks/useCrew.test.js`

Pattern follows the existing `useRaceStats.test.js` / `useTrackRecorder.test.js` style — `vitest` + `@testing-library/react` + a mocked `apiFetch`. Each suite covers: initial load → success, initial load → error, create updates local cache without refetch, remove updates local cache without refetch.

**Verification caveat:** per my memory, vitest can't run in this sandbox (bus error). So **I cannot prove these pass** before you wake up. I'll write them by carefully matching the existing test file shape and you'll run `npm test` on Windows in the morning. If any fail, they're isolated new files — `git rm` them.

---

### 6. Frontend: RaceCard enhancement (SEE WIREFRAME)

**Files touched:**
- `frontend/src/RacesListView.jsx` — enhance `RaceCard` to show start date + raced badge; add filter/sort bar to the list header.
- New: `frontend/src/lib/formatRaceDate.js` — tiny pure helper for "Sat 30 May · 14:00" formatting.
- New: `frontend/src/lib/formatRaceDate.test.js` — unit tests for the helper.

**Changes per the wireframe I just showed you:**
1. RaceCard: add a "Raced" pill when `stats_available || mark_passes.length > 0`. Same logic that currently decides whether to render the Stats button.
2. RaceCard: add a one-line date row above the existing `mode · boat_class · marks` line, only when `race.start_at` is set. Falls back to "No start time" in muted text if you'd rather (your call — current wireframe just omits the row when absent).
3. List header: add a filter bar above the `<ul>` — name search (client-side filter on `race.name`), boat-class dropdown (populated from the loaded races, plus "All boats"), raced/planned/all dropdown, sort dropdown ("Newest first" = current `created_at DESC` from the API; "Start date" = client sort on `start_at`).
4. All filtering is client-side — no API contract change, no router change, no new endpoints.

**Risk / blast radius:**
- Existing tests for `RacesListView` don't exist (only hook tests do), so I can't break them.
- All changes are additive in one component file + one small new pure helper. Easy `git checkout -- frontend/src/RacesListView.jsx` if you hate it.
- No new dependencies.
- Styling uses the existing `styles` object pattern + the existing CSS variables (`--ink`, `--paper`, `--rule`, etc.) for consistency with the rest of the screen.

**What I will NOT do here:**
- Refactor `RacesListView` into smaller components (tempting but scope creep).
- Touch `RaceEditor.jsx`.
- Add server-side filtering / pagination (premature; you have maybe dozens of races).
- Persist filter state to URL or localStorage (a follow-up if you like it).

**Verification caveat:** same as #5 — vitest doesn't run here, so the date-helper tests and any RaceCard tests I write are unverified until Windows. The component renders are eyeball-only on your end.

---

### 7. Stretch: draft the privacy policy for Play / App Store background-location

**Deliverable:** `sailline -docs/2026-05-26_privacy-policy-draft.md`

Phase 6 of the dev plan blocks on a privacy policy URL. The text is mine to draft; you host it. Won't ship it overnight, just have the draft ready. Covers: what data we collect (GPS, IMU optional, user profile from Firebase, boat polars), why (race telemetry/replay/routing), retention, third parties (Firebase, Mapbox, Anthropic, GCP, Transistorsoft SDK), user rights, contact email.

If I run out of useful work, this is the fallback. If I don't get to it, you've lost nothing — it's not on the Phase 2 critical path.

---

## Explicit non-goals tonight

- **No migrations.** Anything that touches `backend/migrations/` waits for you.
- **No deploys / no `git push`.** I'll commit locally so you can review the diff in the morning, OR leave the work uncommitted with a note — your call below.
- **Frontend changes are scoped and unverified locally.** Items #5 and #6 land code; vitest can't run in this sandbox, so the new tests and the RaceCard changes are eyeball/`npm test`-on-Windows verifications. Both are easy to revert (`git checkout` / `git rm`).
- **No mobile changes.** No EAS access from here.
- **No Phase 2 implementation code.** Plan only — that's the project rule.
- **No edits to `Development plan.docx`.** Locked file under your end-of-session ritual.

---

## Verification approach

For the test files (#2, #3): I will run `pytest` for *just those new files* in the sandbox if the backend deps install cleanly. Per memory, the bash mount of E:\ can serve stale copies, so I'll re-read each file via the Read tool after writing to confirm it's actually persisted, and write any pytest output into the morning summary doc rather than trusting it blindly.

For the plan docs (#1, #4, #5): no code, no tests needed — they're written artifacts for your review.

---

## What you need to decide before approving

1. **Commit vs leave uncommitted?** I lean toward **leave uncommitted** so you can `git diff` and stage piecemeal in the morning. If you'd rather have me commit each deliverable as its own commit with a clear message, say so.
2. **Scope cut?** If only one or two of {#1, #2, #3, #4, #5} are worth it to you, tell me which to drop. I'd protect #1 (Phase 2 plan) as the most valuable.
3. **Anything I'm missing?** If there's a known issue I haven't surfaced — a bug you've been meaning to file, a hook you want refactored — name it and I'll swap it in.

---

## Morning runbook (queued for you)

When you're back, in roughly this order:
1. `git status` in `E:\Personal\Coding\SailLine` — see what I touched.
2. Read this plan's deliverables list, skim the new `.md` files.
3. Read + approve/redline the Phase 2 plan.
4. On Windows: `cd backend && pytest tests/test_avatars_service.py tests/test_job_trigger.py -v` to confirm the new backend tests pass on the real box.
5. On Windows: `cd frontend && npm test` — runs the new hook tests (#5) and the date-helper tests (#6). If any new test fails, the failing file is isolated — delete it or fix it.
6. On Windows: `cd frontend && npm run dev`, open `/races`, eyeball the RaceCard + filter bar. If you hate it, `git checkout -- frontend/src/RacesListView.jsx` and `git rm frontend/src/lib/formatRaceDate.*` reverts cleanly.
6. If everything looks good: stage, commit with my draft messages (in the morning summary doc I'll leave), push.
