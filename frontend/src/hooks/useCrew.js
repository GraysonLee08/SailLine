// useCrew — CRUD on boat crew + invites + redeem.
//
// One hook covers the small surface of /api/boats/{id}/crew + /invites
// because the BoatEditor's crew section needs them together. The
// redeem call (used by AcceptInviteView) is exposed as a standalone
// helper so it doesn't require a boatId at construction time.

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api";

export function useCrew(boatId) {
  const [members, setMembers] = useState(null);   // list or null
  const [invites, setInvites] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    if (!boatId) return;
    setLoading(true);
    setError(null);
    try {
      const [m, i] = await Promise.all([
        apiFetch(`/api/boats/${boatId}/crew`),
        apiFetch(`/api/boats/${boatId}/invites`).catch(() => []),
        // Inviter list is only visible to owners; .catch swallows 404
        // so a viewer just sees an empty list.
      ]);
      setMembers(m);
      setInvites(i);
    } catch (e) {
      setError(e.message || String(e));
    } finally {
      setLoading(false);
    }
  }, [boatId]);

  useEffect(() => {
    if (!boatId) {
      setMembers(null);
      setInvites(null);
      return;
    }
    refresh();
  }, [boatId, refresh]);

  const updateRole = useCallback(
    async (memberUid, role) => {
      await apiFetch(`/api/boats/${boatId}/crew/${memberUid}`, {
        method: "PATCH",
        body: { role },
      });
      await refresh();
    },
    [boatId, refresh],
  );

  const removeMember = useCallback(
    async (memberUid) => {
      await apiFetch(`/api/boats/${boatId}/crew/${memberUid}`, {
        method: "DELETE",
      });
      await refresh();
    },
    [boatId, refresh],
  );

  const createInvite = useCallback(
    async ({ role, email, expiresInDays }) => {
      const body = { role };
      if (email) body.email = email;
      if (expiresInDays) body.expires_in_days = expiresInDays;
      const created = await apiFetch(`/api/boats/${boatId}/invites`, {
        method: "POST",
        body,
      });
      await refresh();
      return created;
    },
    [boatId, refresh],
  );

  const revokeInvite = useCallback(
    async (code) => {
      await apiFetch(`/api/boats/${boatId}/invites/${code}`, {
        method: "DELETE",
      });
      await refresh();
    },
    [boatId, refresh],
  );

  return {
    members,
    invites,
    loading,
    error,
    refresh,
    updateRole,
    removeMember,
    createInvite,
    revokeInvite,
  };
}


// Standalone redeem — no boatId required at construction time.
// Used by AcceptInviteView.
export async function redeemInvite(code) {
  return apiFetch("/api/invites/redeem", {
    method: "POST",
    body: { code },
  });
}
