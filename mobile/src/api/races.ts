// api/races.ts — typed wrappers for the /api/races endpoints.
//
// Kept thin on purpose: each function is a single apiFetch call with a
// typed return. No retry, no caching, no error massaging — the calling
// hook/screen decides how to surface failure. Mirrors the contract of
// frontend/src/hooks/useRaces.js so a future shared-package extraction
// (packages/shared/src/api/races.js) is a near-mechanical move.

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
  return data ?? [];
}

/** Fetch a single race by id. Throws on 404. */
export async function getRace(id: string): Promise<Race> {
  const data = await apiFetch<Race>(`/api/races/${id}`);
  if (!data) throw new Error(`Race ${id} returned no body`);
  return data;
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
  return data;
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
  return data;
}
