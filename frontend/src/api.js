// Thin wrapper around fetch that attaches the current user's
// Firebase ID token as a Bearer credential. Centralised so future
// endpoints don't each re-implement auth.

import { auth } from "./firebase";

// In production, paths are relative — Firebase Hosting rewrites /api/** to
// the sailline-api Cloud Run service (same-origin, no CORS).
// In local dev, set VITE_API_URL=http://localhost:8080 in .env.local.
const API_URL = import.meta.env.VITE_API_URL || "";

export async function apiFetch(path, { method = "GET", body } = {}) {
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

  // 204 No Content (e.g. DELETE) — nothing to parse.
  if (res.status === 204) return null;
  return res.json();
}
