// BoatsView — list of the user's boats. Mirrors RacesListView in
// layout. Each row has Edit / Delete / Set default.
//
// Default-boat selection is a single-select across rows; the radio
// buttons sync to the profile via PATCH /api/users/me.

import { useEffect, useState } from "react";

import { apiFetch } from "./api";
import { useBoats } from "./hooks/useBoats";

export default function BoatsView({ onBack, onCreate, onEdit }) {
  const { boats, error, remove } = useBoats();
  const [defaultBoatId, setDefaultBoatId] = useState(null);
  const [profileError, setProfileError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/users/me")
      .then((p) => !cancelled && setDefaultBoatId(p.default_boat_id ?? null))
      .catch((e) => !cancelled && setProfileError(e.message || String(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  const setDefault = async (id) => {
    setDefaultBoatId(id);
    try {
      await apiFetch("/api/users/me", {
        method: "PATCH",
        body: { default_boat_id: id },
      });
    } catch (e) {
      setProfileError(e.message || String(e));
    }
  };

  return (
    <div style={styles.shell}>
      <header style={styles.header}>
        <button onClick={onBack} style={styles.backBtn} aria-label="Back">
          ← Back
        </button>
        <h1 style={styles.title}>Boats</h1>
        <button onClick={onCreate} style={styles.newBtn}>
          + Add boat
        </button>
      </header>

      <main style={styles.body}>
        {error && <div style={styles.error}>Couldn't load boats: {error}</div>}
        {profileError && (
          <div style={styles.error}>Profile error: {profileError}</div>
        )}
        {boats === null && <div style={styles.muted}>Loading…</div>}

        {boats && boats.length === 0 && (
          <div style={styles.empty}>
            <p style={styles.emptyTitle}>No boats yet.</p>
            <p style={styles.emptyHint}>
              Add your boat to record handicap ratings and see corrected
              times after each race.
            </p>
            <button onClick={onCreate} style={styles.emptyBtn}>
              Add a boat
            </button>
          </div>
        )}

        {boats && boats.length > 0 && (
          <ul style={styles.list}>
            {boats.map((b) => (
              <BoatCard
                key={b.id}
                boat={b}
                isDefault={b.id === defaultBoatId}
                onSetDefault={() => setDefault(b.id)}
                onEdit={() => onEdit(b.id)}
                onDelete={async () => {
                  if (
                    !confirm(`Delete "${b.name}"? This can't be undone.`)
                  )
                    return;
                  try {
                    if (b.id === defaultBoatId) {
                      await setDefault(null);
                    }
                    await remove(b.id);
                  } catch (e) {
                    alert(`Couldn't delete: ${e.message || e}`);
                  }
                }}
              />
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}


function BoatCard({ boat, isDefault, onSetDefault, onEdit, onDelete }) {
  const rating = boat.hcp ?? boat.dhcp ?? boat.nshcp ?? boat.dnshcp ?? null;
  return (
    <li style={styles.card}>
      <div style={styles.cardMain}>
        <div style={styles.cardHeadRow}>
          <h3 style={styles.cardName}>{boat.name}</h3>
          {isDefault && <span style={styles.defaultBadge}>Default</span>}
        </div>
        <div style={styles.cardMeta}>
          {boat.sail_number && (
            <>
              <span>{boat.sail_number}</span>
              <span style={styles.metaSep}>·</span>
            </>
          )}
          {boat.yacht_type && (
            <>
              <span>{boat.yacht_type}</span>
              <span style={styles.metaSep}>·</span>
            </>
          )}
          <span>
            {rating != null
              ? `Rating: ${rating}`
              : "No rating set"}
          </span>
          {boat.mwphrf_region != null && (
            <>
              <span style={styles.metaSep}>·</span>
              <span>MWPHRF Region {boat.mwphrf_region}</span>
            </>
          )}
        </div>
      </div>
      <div style={styles.cardActions}>
        <label style={styles.defaultRadio}>
          <input
            type="radio"
            checked={isDefault}
            onChange={onSetDefault}
          />
          <span>Default</span>
        </label>
        <button onClick={onEdit} style={styles.editBtn}>
          Edit
        </button>
        <button onClick={onDelete} style={styles.deleteBtn} aria-label="Delete boat">
          Delete
        </button>
      </div>
    </li>
  );
}


const styles = {
  shell: {
    position: "absolute",
    inset: 0,
    background: "var(--paper, #f8f8f7)",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 16,
    padding: "20px 32px",
    borderBottom: "1px solid var(--rule, #eaeaea)",
  },
  backBtn: {
    border: "1px solid var(--rule, #eaeaea)",
    background: "var(--paper, #fff)",
    borderRadius: "var(--r-sm, 6px)",
    padding: "8px 14px",
    fontSize: 14,
    cursor: "pointer",
  },
  title: { margin: 0, fontSize: 28, flex: 1 },
  newBtn: {
    border: "none",
    background: "var(--ink, #16161a)",
    color: "var(--paper, #fff)",
    borderRadius: "var(--r-md, 8px)",
    padding: "10px 20px",
    fontSize: 14,
    fontWeight: 500,
    cursor: "pointer",
  },
  body: { flex: 1, overflowY: "auto", padding: "20px 32px" },
  error: {
    padding: 12, marginBottom: 16,
    border: "1px solid #f0c4c4", background: "#fdecec", color: "#8a1f1f",
    borderRadius: 8,
  },
  muted: { padding: 28, color: "#6a6a6f", textAlign: "center" },
  empty: { padding: 60, textAlign: "center" },
  emptyTitle: { fontSize: 18, color: "#3a3a40", margin: 0 },
  emptyHint: { fontSize: 14, color: "#6a6a6f", margin: "8px 0 18px" },
  emptyBtn: {
    border: "none", background: "var(--ink, #16161a)",
    color: "var(--paper, #fff)",
    borderRadius: 8, padding: "10px 20px", cursor: "pointer",
  },
  list: { listStyle: "none", padding: 0, margin: 0, display: "flex", flexDirection: "column", gap: 12 },
  card: {
    background: "white",
    border: "1px solid var(--rule, #eaeaea)",
    borderRadius: 10,
    padding: "14px 16px",
    display: "flex",
    alignItems: "center",
    gap: 16,
  },
  cardMain: { flex: 1, minWidth: 0 },
  cardHeadRow: { display: "flex", alignItems: "center", gap: 10 },
  cardName: { margin: 0, fontSize: 16, fontWeight: 600 },
  defaultBadge: {
    fontSize: 10,
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    color: "#1a73e8",
    border: "1px solid #1a73e8",
    borderRadius: 4,
    padding: "1px 6px",
    fontWeight: 600,
  },
  cardMeta: {
    fontSize: 12,
    color: "#6a6a6f",
    marginTop: 4,
    display: "flex",
    flexWrap: "wrap",
    gap: 4,
  },
  metaSep: { color: "#c8c8cc" },
  cardActions: {
    display: "flex",
    gap: 8,
    alignItems: "center",
    flexShrink: 0,
  },
  defaultRadio: {
    display: "flex",
    alignItems: "center",
    gap: 4,
    fontSize: 12,
    cursor: "pointer",
  },
  editBtn: {
    padding: "6px 12px",
    fontSize: 13,
    border: "1px solid var(--rule, #d8d8de)",
    background: "white",
    borderRadius: 6,
    cursor: "pointer",
  },
  deleteBtn: {
    padding: "6px 12px",
    fontSize: 13,
    border: "1px solid #e0a0a0",
    background: "white",
    color: "#8a1f1f",
    borderRadius: 6,
    cursor: "pointer",
  },
};
