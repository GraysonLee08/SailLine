// useRaces — list-level data for the races screen. Loads on mount, exposes
// create/remove that locally update the cache so the UI reflects mutations
// without an extra round-trip. The editor screen has its own load-by-id
// path (see RaceEditor.jsx) since it doesn't depend on the list being loaded.

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api";

export function useRaces() {
  const [races, setRaces] = useState(null); // null → loading, [] → empty
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    try {
      const data = await apiFetch("/api/races");
      setRaces(data);
      setError(null);
    } catch (e) {
      setError(e.message || String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const create = useCallback(async (race) => {
    const created = await apiFetch("/api/races", { method: "POST", body: race });
    setRaces((prev) => [created, ...(prev || [])]);
    return created;
  }, []);

  const remove = useCallback(async (id) => {
    await apiFetch(`/api/races/${id}`, { method: "DELETE" });
    setRaces((prev) => (prev || []).filter((r) => r.id !== id));
  }, []);

  // The editor calls PATCH directly via apiFetch and then refreshes the list
  // via this hook on its way back to the list view. Keeping update out of
  // the hook avoids needing to thread the editor's full state through here.

  return { races, error, refresh, create, remove };
}
