// recorderDebrief.ts — POST /api/races/{race_id}/recorder-debrief.
//
// Best-effort wrapper around apiFetch. The recorder calls this once
// per stop() and never blocks teardown on its outcome.

import { apiFetch } from "../api";

import type { RecorderDebrief } from "../recorder/debrief";

export type RecorderDebriefAck = {
  id: string;
  created_at: string;
};

/**
 * POST a debrief. Resolves with the server's ack (id + created_at)
 * on 201, throws on any non-2xx. Callers are expected to swallow
 * errors — the debrief is diagnostic, not load-bearing.
 */
export async function postRecorderDebrief(
  raceId: string,
  payload: RecorderDebrief,
): Promise<RecorderDebriefAck> {
  const ack = await apiFetch<RecorderDebriefAck>(
    `/api/races/${raceId}/recorder-debrief`,
    {
      method: "POST",
      body: payload,
    },
  );
  // apiFetch types return as T|null because 204 is possible. We post
  // to an endpoint that returns 201 with a body; null here would be
  // a server contract violation.
  if (ack === null) {
    throw new Error("recorder-debrief: server returned empty body");
  }
  return ack;
}
