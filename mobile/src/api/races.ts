// api/races.ts — typed wrappers for the /api/races endpoints.
//
// Kept thin on purpose: each function is a single apiFetch call with a
// typed return. No retry, no caching, no error massaging — the calling
// hook/screen decides how to surface failure. Mirrors the contract of
// frontend/src/hooks/useRaces.js so a future shared-package extraction
// (packages/shared/src/api/races.js) is a near-mechanical move.
//
// 2026-06-03 — getRace() now defensively normalises `mark_passes` on
// the way out. Backend has been observed (studio_results_20260602_1741.json)
// returning the JSONB column as a string of a JSON array instead of as
// an array. That's a backend serialisation bug to fix separately; this
// shim keeps the mobile UI honest in the meantime so the in-race pill
// row doesn't blink-empty on every poll.

import { apiFetch } from "../api";
import type { Race, RaceMark } from "../types";

/**
 * List all races visible to the signed-in user (their own + crew-shared).
 * Server returns them ordered by created_at DESC.
 */
export async function listRaces(): Promise<Race[]> {
  const data = await apiFetch<Race[]>("/api/races");
  // Server can't return null for a list endpoint (it 200s with []), but
  // apiFetch's signature allows null for 204s. Normalise defensively so
  // callers never have to nullcheck.
  return (data ?? []).map(normaliseRace);
}

/** Fetch a single race by id. Throws on 404. */
export async function getRace(id: string): Promise<Race> {
  const data = await apiFetch<Race>(`/api/races/${id}`);
  if (!data) throw new Error(`Race ${id} returned no body`);
  return normaliseRace(data);
}

/**
 * Payload shape accepted by POST /api/races and PATCH /api/races/{id}.
 * Matches the webapp RaceEditor.jsx save payload (see frontend/src/
 * RaceEditor.jsx::handleSave). Optional fields are intentionally
 * narrowed to what mobile sets today — widen as more sections of the
 * editor land on mobile.
 */
export type RacePayload = {
  name: string;
  mode: "inshore" | "distance";
  boat_class: string;
  marks: RaceMark[];
  /** Combined ISO UTC string ("2026-06-01T13:30:00.000Z") or null. */
  start_at: string | null;
  /** Recording feature — mobile keeps the column default (true) so
      existing race rows don't lose their value when re-saved. */
  auto_start_enabled: boolean;
  /** Per-race boat link for corrected-time / handicap. */
  boat_id: string | null;
  /** Spinnaker vs non-spinnaker handicap class. */
  uses_spinnaker: boolean;
};

/** Create a new race. Returns the persisted row (with id, timestamps). */
export async function createRace(payload: RacePayload): Promise<Race> {
  const data = await apiFetch<Race>("/api/races", {
    method: "POST",
    body: payload,
  });
  if (!data) throw new Error("POST /api/races returned no body");
  return normaliseRace(data);
}

/** Update an existing race. Returns the persisted row. */
export async function updateRace(
  id: string,
  payload: RacePayload,
): Promise<Race> {
  const data = await apiFetch<Race>(`/api/races/${id}`, {
    method: "PATCH",
    body: payload,
  });
  if (!data) throw new Error(`PATCH /api/races/${id} returned no body`);
  return normaliseRace(data);
}

/**
 * Manually record a mark pass for a race. Used when the auto-detection
 * missed a mark and the sailor confirms via the "Yes, missed it"
 * notification action. The backend inserts the pass at the current
 * GPS position (or the mark position if no GPS is available) and
 * advances the detector so subsequent marks can be detected.
 *
 * Returns the updated race row with the new mark_passes array.
 */
export async function manualMarkPass(
  raceId: string,
  markIndex: number,
): Promise<Race> {
  const data = await apiFetch<Race>(`/api/races/${raceId}/mark-pass`, {
    method: "POST",
    body: { mark_index: markIndex },
  });
  if (!data) {
    throw new Error(`POST /api/races/${raceId}/mark-pass returned no body`);
  }
  return normaliseRace(data);
}

/**
 * Mark a race as ended and trigger the AI post-race summary.
 * Called from the recording screen's Stop button after recorder.stop()
 * completes. Ensures the AI summary is generated even when marks were
 * missed and the auto-detector never crossed the final mark.
 */
export async function endRace(raceId: string): Promise<Race> {
  const data = await apiFetch<Race>(`/api/races/${raceId}/end`, {
    method: "POST",
  });
  if (!data) {
    throw new Error(`POST /api/races/${raceId}/end returned no body`);
  }
  return normaliseRace(data);
}

/**
 * endRace with retries (2026-07-02). The Beer Can 7.1.2026 race ended
 * with `ended_at` NULL because the single silent endRace call from the
 * Stop handler failed — which meant the ENTIRE post-race pipeline
 * (stats, wind snapshot, AI summary) never ran. This wrapper retries
 * with exponential backoff (1 s, 2 s, 4 s between attempts) before
 * giving up, and the caller is expected to SURFACE the final failure to
 * the user rather than swallow it. The server-side stale-race sweep
 * (workers/race_sweep.py) is the backstop of last resort, but that
 * runs on a schedule — the user shouldn't wait an hour for their
 * summary because of one dropped request.
 */
export async function endRaceWithRetry(
  raceId: string,
  attempts = 3,
): Promise<Race> {
  let lastErr: unknown;
  for (let i = 0; i < attempts; i += 1) {
    try {
      return await endRace(raceId);
    } catch (e) {
      lastErr = e;
      if (i < attempts - 1) {
        await new Promise((r) => setTimeout(r, 1000 * 2 ** i));
      }
    }
  }
  throw lastErr;
}

/** A recorded GPS fix as returned by GET /api/races/{id}/track — mirrors
 *  backend tracks.py::TrackPointOut. Note there is no gps_acc_m on the
 *  wire; callers that need the recorder's LocalPoint shape fill it with
 *  null. */
export type TrackPoint = {
  recorded_at: string;
  lat: number;
  lon: number;
  speed_kts: number | null;
  heading_deg: number | null;
};

/**
 * Fetch every persisted track point for a race, chronological. Used by
 * the Debrief screen's fallback path when the recorder's in-memory
 * breadcrumb isn't available (old race opened from the list, or the app
 * restarted since the recording stopped).
 */
export async function getTrack(raceId: string): Promise<TrackPoint[]> {
  const data = await apiFetch<TrackPoint[]>(`/api/races/${raceId}/track`);
  // The endpoint 200s with [] for a trackless race; normalise apiFetch's
  // 204-null case defensively so callers never nullcheck.
  return data ?? [];
}

/** A persisted mark-rounding event — matches the JSONB shape on
 *  race_sessions.mark_passes and the backend's track-ingest output.
 *  All passes are auto-detected: the manual-pass path was removed
 *  2026-06-08 so the detector is the sole writer (single-writer
 *  guarantee — no index drift). */
export type MarkPass = {
  mark_index: number;
  ts: string;
  lat: number;
  lon: number;
};

// ─── Defensive shape normalisation ─────────────────────────────────────
//
// Backend has occasionally returned `mark_passes` as a JSON string of a
// JSON array instead of as an actual array — a serialisation bug in
// backend/app/routers/races.py (flagged for a separate fix). Until that
// lands we coerce here so the UI doesn't blink-empty on every poll.

let _warnedDoubleEncoded = false;

function coerceMarkPassArray(value: unknown, ctx: string): MarkPass[] {
  if (Array.isArray(value)) return value as MarkPass[];
  if (typeof value === "string" && value.length > 0) {
    try {
      const parsed = JSON.parse(value);
      if (Array.isArray(parsed)) {
        if (!_warnedDoubleEncoded) {
          _warnedDoubleEncoded = true;
          // eslint-disable-next-line no-console
          console.warn(
            `[api/races] mark_passes arrived double-encoded (${ctx}); ` +
              `JSON.parsed it locally. Backend serialisation needs a fix.`,
          );
        }
        return parsed as MarkPass[];
      }
    } catch {
      // fall through to empty array
    }
  }
  return [];
}

/**
 * Normalise a Race row coming off the wire. Today this only patches
 * mark_passes (the known offender); add other coercions here as
 * shape-drift bugs are spotted.
 */
function normaliseRace(race: Race): Race {
  if (race.mark_passes === undefined || race.mark_passes === null) return race;
  const coerced = coerceMarkPassArray(race.mark_passes, `race ${race.id}`);
  // Avoid allocating a new object when the field was already a valid array.
  if (coerced === (race.mark_passes as unknown)) return race;
  return { ...race, mark_passes: coerced };
}
