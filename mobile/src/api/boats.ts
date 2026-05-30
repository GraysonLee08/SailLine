// api/boats.ts — typed wrapper for /api/boats list.
//
// Mobile only needs the dropdown shape today (id + name + sail_number)
// to populate the race-editor "Boat (for handicap)" picker. The full
// BoatOut response from the backend (boats.py BoatOut) is wider — we
// type just what we read.

import { apiFetch } from "../api";

/** Subset of BoatOut used by the editor dropdown. */
export type BoatOption = {
  id: string;
  name: string;
  sail_number?: string | null;
};

/**
 * List boats the signed-in user can see (owned + crewed). Server
 * returns them ordered by created_at DESC. Returns [] on transport
 * errors so the editor can still render with "no boat" as the only
 * choice — failing the whole editor over a missing dropdown is too
 * harsh.
 */
export async function listBoats(): Promise<BoatOption[]> {
  try {
    const data = await apiFetch<BoatOption[]>("/api/boats");
    return data ?? [];
  } catch {
    return [];
  }
}
