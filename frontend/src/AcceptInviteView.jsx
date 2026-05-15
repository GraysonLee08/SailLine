// AcceptInviteView — landing screen when a recipient clicks an
// invite link.
//
// Flow:
//   1. URL carries ?invite=<code>
//   2. AppView detects the param and routes here (after auth gate)
//   3. We show a confirm panel ("Join {boat name}?") with Accept /
//      Cancel buttons
//   4. Accept → POST /api/invites/redeem → success or error
//   5. On success, drop the ?invite= param and navigate to BoatsView
//
// We don't fetch any boat detail before redeeming because the
// recipient might not yet have read access. The success response
// carries boat_id + role; the next BoatsView fetch surfaces the
// freshly-added boat.

import { useState } from "react";

import { redeemInvite } from "./hooks/useCrew";

export default function AcceptInviteView({ code, onAccepted, onCancel }) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(null);   // {boat_id, role}

  const handleAccept = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const res = await redeemInvite(code);
      setSuccess(res);
      // Pause briefly so the user sees the confirmation; AppView
      // navigates onward once acknowledged.
      setTimeout(() => onAccepted?.(res), 600);
    } catch (e) {
      const msg = e.message || String(e);
      // Surface friendlier messages for the well-known states.
      if (msg.includes("404")) {
        setError("This invite link is invalid or has been revoked.");
      } else if (msg.includes("410")) {
        setError("This invite has expired. Ask the owner for a new one.");
      } else if (msg.includes("409")) {
        setError("This invite has already been used.");
      } else {
        setError(msg);
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={styles.shell}>
      <div style={styles.card}>
        <h1 style={styles.title}>You've been invited</h1>
        <p style={styles.subtitle}>
          Accept this invite to join the boat on your SailLine profile.
        </p>
        <div style={styles.codeBlock}>
          <span style={styles.codeLabel}>Invite code</span>
          <code style={styles.code}>{code}</code>
        </div>

        {error && <div style={styles.error}>{error}</div>}
        {success && (
          <div style={styles.success}>
            Joined! Role: <strong>{success.role}</strong>. Taking you to your boats…
          </div>
        )}

        <div style={styles.actions}>
          <button
            onClick={handleAccept}
            disabled={submitting || success}
            style={styles.primary}
          >
            {submitting ? "Joining…" : success ? "Joined" : "Accept invite"}
          </button>
          <button
            onClick={onCancel}
            disabled={submitting}
            style={styles.secondary}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}


const styles = {
  shell: {
    position: "absolute",
    inset: 0,
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    background: "var(--paper, #f8f8f7)",
    padding: 20,
  },
  card: {
    background: "white",
    border: "1px solid var(--rule, #eaeaea)",
    borderRadius: 12,
    padding: 28,
    maxWidth: 460,
    width: "100%",
    display: "flex",
    flexDirection: "column",
    gap: 14,
  },
  title: { margin: 0, fontSize: 22, fontWeight: 600 },
  subtitle: { margin: 0, fontSize: 14, color: "#6a6a6f", lineHeight: 1.5 },
  codeBlock: {
    display: "flex",
    flexDirection: "column",
    gap: 4,
    padding: 12,
    background: "#f4f4f3",
    borderRadius: 8,
  },
  codeLabel: {
    fontSize: 11,
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    color: "#6a6a6f",
  },
  code: {
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
    fontSize: 14,
    color: "#16161a",
    wordBreak: "break-all",
  },
  error: {
    padding: 10,
    border: "1px solid #f0c4c4",
    background: "#fdecec",
    color: "#8a1f1f",
    borderRadius: 8,
    fontSize: 13,
  },
  success: {
    padding: 10,
    border: "1px solid #b9dcb6",
    background: "#eaf4e9",
    color: "#2c632a",
    borderRadius: 8,
    fontSize: 13,
  },
  actions: { display: "flex", gap: 8, marginTop: 4 },
  primary: {
    flex: 1,
    padding: "10px 18px",
    background: "#16161a",
    color: "white",
    border: "none",
    borderRadius: 8,
    fontSize: 14,
    fontWeight: 500,
    cursor: "pointer",
  },
  secondary: {
    padding: "10px 18px",
    background: "white",
    color: "#16161a",
    border: "1px solid #d8d8de",
    borderRadius: 8,
    fontSize: 14,
    cursor: "pointer",
  },
};
