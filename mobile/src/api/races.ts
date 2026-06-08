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
