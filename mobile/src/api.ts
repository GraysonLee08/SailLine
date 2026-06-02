// api.ts — RN fetch wrapper that attaches the Firebase ID token.
//
// Mirrors frontend/src/api.js, with one structural difference: the web
// app calls relative `/api/**` paths that Firebase Hosting rewrites to
// Cloud Run (same-origin, no CORS). The mobile app has no such rewrite,
// so it calls the Cloud Run origin DIRECTLY. Native fetch sends no
// `Origin` header, so the backend's browser CORS middleware does not
// gate these requests (verified against the deployed API).
//
// Base URL: set EXPO_PUBLIC_API_URL to your LAN dev server
// (e.g. http://192.168.1.50:8080) when testing against a local backend;
// it defaults to production Cloud Run otherwise.

import { auth } from "./firebase";

const PROD_API_URL =
  "https://sailline-api-105706282249.us-central1.run.app";

/**
 * Base URL the mobile app posts to. Exported so the Phase 4 native
 * uploader (Transistorsoft's HTTP layer) can compose its per-race
 * telemetry URL — `${API_URL}/api/races/{race_id}/telemetry`.
 */
export const API_URL = process.env.EXPO_PUBLIC_API_URL || PROD_API_URL;

type ApiOptions = {
  method?: string;
  body?: unknown;
};

export async function apiFetch<T = unknown>(
  path: string,
  { method = "GET", body }: ApiOptions = {},
): Promise<T | null> {
  const user = auth.currentUser;
  if (!user) throw new Error("Not authenticated");

  const token = await user.getIdToken();
  const res = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`API ${res.status}: ${text || res.statusText}`);
  }

  if (res.status === 204) return null;
  return (await res.json()) as T;
}
