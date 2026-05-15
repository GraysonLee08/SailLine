// useBoats — CRUD over /api/boats plus cert upload.
//
// Mirrors useRaces in shape: a list, a refresh, plus single-record
// operations the BoatsView and BoatEditor call. The cert-upload call
// is its own method because it needs multipart and returns parsed
// fields that the caller wants synchronously to pre-fill the editor.

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api";
import { auth } from "../firebase";

const API_BASE = import.meta.env.VITE_API_URL || "";

export function useBoats() {
  const [boats, setBoats] = useState(null);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const res = await apiFetch("/api/boats");
      setBoats(res);
      setError(null);
    } catch (e) {
      setError(e.message || String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const create = useCallback(async (payload) => {
    const created = await apiFetch("/api/boats", {
      method: "POST",
      body: payload,
    });
    setBoats((prev) => (prev ? [created, ...prev] : [created]));
    return created;
  }, []);

  const update = useCallback(async (id, patch) => {
    const updated = await apiFetch(`/api/boats/${id}`, {
      method: "PATCH",
      body: patch,
    });
    setBoats((prev) =>
      prev ? prev.map((b) => (b.id === id ? updated : b)) : prev,
    );
    return updated;
  }, []);

  const remove = useCallback(async (id) => {
    await apiFetch(`/api/boats/${id}`, { method: "DELETE" });
    setBoats((prev) => (prev ? prev.filter((b) => b.id !== id) : prev));
  }, []);

  // Cert upload — multipart, can't go through the JSON-only apiFetch
  // wrapper. Build the request manually but reuse the auth token
  // logic.
  const uploadCert = useCallback(async (id, file) => {
    const user = auth.currentUser;
    if (!user) throw new Error("Not authenticated");
    const token = await user.getIdToken();
    const fd = new FormData();
    fd.append("file", file);
    const res = await fetch(`${API_BASE}/api/boats/${id}/cert`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: fd,
    });
    if (!res.ok) {
      const txt = await res.text().catch(() => "");
      throw new Error(`Upload failed (${res.status}): ${txt}`);
    }
    return res.json();
  }, []);

  return { boats, error, refresh, create, update, remove, uploadCert };
}
