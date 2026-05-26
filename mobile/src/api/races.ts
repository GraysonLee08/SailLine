// api/races.ts — typed wrappers for the /api/races endpoints.
//
// Kept thin on purpose: each function is a single apiFetch call with a
// typed return. No retry, no caching, no error massaging — the calling
// hook/screen decides how to surface failure. Mirrors the contract of
// frontend/src/hooks/useRaces.js so a future shared-package extraction
// (packages/shared/src/api/races.js) is a near-mechanical move.

import { apiFetch } from "../api";
import type { Race } from "../types";

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
