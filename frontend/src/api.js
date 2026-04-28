// Thin wrapper around fetch that attaches the current user's
// Firebase ID token as a Bearer credential. Centralised so future
// endpoints don't each re-implement auth.

import { auth } from "./firebase";

const API_URL = import.meta.env.VITE_API_URL || "https://sailline-api-105706282249.us-central1.run.app";

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
  return res.json();
}
