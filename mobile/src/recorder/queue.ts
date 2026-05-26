// queue.ts — AsyncStorage-backed offline GPS queue, per race.
//
// The RN analogue of the localStorage queue in the web recorder. Points
// stay here until the server 200s the batch they were in, so a dropped
// network connection (common on the water) never loses fixes — they
// accumulate locally and drain on the next successful flush. If the app
// is closed and reopened mid-race, the queue is restored on start.
//
// Per-race scoping mirrors the web keys exactly so the contract is the
// same across platforms: `sailline.trackQueue.<raceId>`.

import AsyncStorage from "@react-native-async-storage/async-storage";

import type { LocalPoint } from "./backgroundGeolocation";

const GPS_PREFIX = "sailline.trackQueue.";

function gpsKey(raceId: string): string {
  return `${GPS_PREFIX}${raceId}`;
}

/** Load the persisted GPS queue for a race. Returns [] on any error. */
export async function loadQueue(raceId: string): Promise<LocalPoint[]> {
  try {
    const raw = await AsyncStorage.getItem(gpsKey(raceId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as LocalPoint[]) : [];
  } catch {
    return [];
  }
}

/** Persist the GPS queue for a race. Best-effort; swallows errors. */
export async function saveQueue(
  raceId: string,
  points: LocalPoint[],
): Promise<void> {
  try {
    await AsyncStorage.setItem(gpsKey(raceId), JSON.stringify(points));
  } catch {
    // Storage full / disabled — best effort, in-memory ref is the
    // primary copy during an active session.
  }
}

/** Remove the persisted queue for a race (called once it drains clean). */
export async function clearQueue(raceId: string): Promise<void> {
  try {
    await AsyncStorage.removeItem(gpsKey(raceId));
  } catch {
    // ignore
  }
}
