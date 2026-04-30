// RacesListView — the user's saved race plans. Entry point to the editor.
//
// Hands navigation back to AppView via callbacks rather than owning routing
// itself, which keeps this component dumb and reusable.

import { useRaces } from "./hooks/useRaces";

export default function RacesListView({ onBack, onOpen, onCreate }) {
  const { races, error, remove } = useRaces();

  return (
    <div style={styles.shell}>
      <header style={styles.header}>
        <button onClick={onBack} style={styles.backBtn} aria-label="Back to map">
          ← Map
        </button>
        <h1 style={styles.title}>Races</h1>
        <button onClick={onCreate} style={styles.newBtn}>
          + New race
        </button>
      </header>

      <main style={styles.body}>
        {error && <div style={styles.error}>Couldn't load races: {error}</div>}

        {races === null && <div style={styles.muted}>Loading…</div>}

        {races && races.length === 0 && (
          <div style={styles.empty}>
            <p style={styles.emptyTitle}>No races yet.</p>
            <p style={styles.emptyHint}>
              Plan your first race — drop the start, marks, and finish on the
              map and save.
            </p>
            <button onClick={onCreate} style={styles.emptyBtn}>
              Create a race
            </button>
          </div>
        )}

        {races && races.length > 0 && (
          <ul style={styles.list}>
            {races.map((r) => (
              <RaceCard
                key={r.id}
                race={r}
                onOpen={() => onOpen(r.id)}
                onDelete={async () => {
                  if (!confirm(`Delete "${r.name}"? This can't be undone.`)) return;
                  try {
                    await remove(r.id);
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

function RaceCard({ race, onOpen, onDelete }) {
  return (
    <li style={styles.card}>
      <div style={styles.cardMain} onClick={onOpen} role="button" tabIndex={0}>
        <h3 style={styles.cardName}>{race.name}</h3>
        <div style={styles.cardMeta}>
          <span style={styles.badge}>{race.mode}</span>
          <span style={styles.metaSep}>·</span>
          <span>{race.boat_class}</span>
          <span style={styles.metaSep}>·</span>
          <span>{race.marks.length} {race.marks.length === 1 ? "mark" : "marks"}</span>
        </div>
      </div>
      <div style={styles.cardActions}>
        <button onClick={onOpen} style={styles.editBtn}>Edit</button>
        <button onClick={onDelete} style={styles.deleteBtn} aria-label="Delete race">
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
    background: "var(--paper)",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 16,
    padding: "20px 32px",
    borderBottom: "1px solid var(--rule)",
    background: "var(--paper)",
  },
  backBtn: {
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    padding: "8px 14px",
    fontSize: 14,
    color: "var(--ink)",
    cursor: "pointer",
  },
  title: {
    margin: 0,
    fontSize: 28,
    flex: 1,
  },
  newBtn: {
    border: "none",
    background: "var(--ink)",
    color: "var(--paper)",
    borderRadius: "var(--r-md)",
    padding: "10px 20px",
    fontSize: 14,
    fontWeight: 500,
    cursor: "pointer",
  },
  body: {
    flex: 1,
    overflowY: "auto",
    padding: "32px",
    maxWidth: 900,
    width: "100%",
    margin: "0 auto",
    boxSizing: "border-box",
  },
  error: {
    padding: "12px 16px",
    background: "rgba(214, 59, 31, 0.08)",
    color: "var(--error)",
    borderRadius: "var(--r-sm)",
    marginBottom: 16,
    fontSize: 14,
  },
  muted: {
    color: "var(--ink-3)",
    fontSize: 14,
  },
  empty: {
    textAlign: "center",
    padding: "80px 24px",
  },
  emptyTitle: {
    fontSize: 18,
    margin: "0 0 8px",
    color: "var(--ink)",
  },
  emptyHint: {
    color: "var(--ink-3)",
    fontSize: 14,
    margin: "0 0 24px",
    maxWidth: 360,
    marginLeft: "auto",
    marginRight: "auto",
    lineHeight: 1.5,
  },
  emptyBtn: {
    border: "none",
    background: "var(--ink)",
    color: "var(--paper)",
    borderRadius: "var(--r-md)",
    padding: "12px 24px",
    fontSize: 14,
    fontWeight: 500,
    cursor: "pointer",
  },
  list: {
    listStyle: "none",
    padding: 0,
    margin: 0,
    display: "flex",
    flexDirection: "column",
    gap: 12,
  },
  card: {
    display: "flex",
    alignItems: "center",
    gap: 16,
    padding: "20px 24px",
    border: "1px solid var(--rule)",
    borderRadius: "var(--r-md)",
    background: "var(--paper)",
  },
  cardMain: {
    flex: 1,
    cursor: "pointer",
    minWidth: 0,
  },
  cardName: {
    margin: "0 0 6px",
    fontSize: 16,
    color: "var(--ink)",
  },
  cardMeta: {
    display: "flex",
    alignItems: "center",
    gap: 8,
    color: "var(--ink-3)",
    fontSize: 13,
  },
  badge: {
    background: "var(--ink-bg, rgba(22,22,26,0.05))",
    color: "var(--ink)",
    borderRadius: 999,
    padding: "2px 10px",
    fontSize: 11,
    textTransform: "uppercase",
    letterSpacing: "0.04em",
    fontWeight: 500,
  },
  metaSep: {
    color: "var(--ink-4)",
  },
  cardActions: {
    display: "flex",
    gap: 8,
    flexShrink: 0,
  },
  editBtn: {
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    padding: "8px 14px",
    fontSize: 13,
    color: "var(--ink)",
    cursor: "pointer",
  },
  deleteBtn: {
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    padding: "8px 14px",
    fontSize: 13,
    color: "var(--error)",
    cursor: "pointer",
  },
};
