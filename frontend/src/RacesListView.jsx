// RacesListView — the user's saved race plans. Entry point to both the
// map view (primary action: load a race onto the map) and the editor
// (secondary: edit the plan).
//
// Hands navigation back to AppView via callbacks rather than owning
// routing itself, which keeps this component dumb and reusable.
//
// 2026-05-26: added a client-side filter/sort bar above the list (name
// search, boat-class filter, raced/planned filter, sort). All filtering
// happens locally on the loaded `races` array — no API change. The card
// now also surfaces the race's start_at and a "Raced" pill when stats
// are available.

import { useMemo, useState } from "react";

import { useRaces } from "./hooks/useRaces";
import { formatRaceDate } from "./lib/formatRaceDate";

export default function RacesListView({
  onBack, onOpen, onEdit, onCreate, onViewStats, currentUid,
}) {
  const { races, error, remove } = useRaces();

  // ── Filter / sort state ────────────────────────────────────────────
  // All client-side. Defaults preserve the previous behaviour (server
  // returns races ORDER BY created_at DESC, so "Newest first" is a
  // no-op against the loaded list).
  const [search, setSearch] = useState("");
  const [classFilter, setClassFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all"); // all | raced | planned
  const [sortBy, setSortBy] = useState("newest"); // newest | start_at

  // Distinct boat classes from the loaded list, for the dropdown.
  const availableClasses = useMemo(() => {
    if (!races) return [];
    const set = new Set(races.map((r) => r.boat_class).filter(Boolean));
    return Array.from(set).sort();
  }, [races]);

  const filteredSorted = useMemo(() => {
    if (!races) return null;
    const q = search.trim().toLowerCase();
    const filtered = races.filter((r) => {
      if (q && !(r.name || "").toLowerCase().includes(q)) return false;
      if (classFilter !== "all" && r.boat_class !== classFilter) return false;
      const raced = isRaced(r);
      if (statusFilter === "raced" && !raced) return false;
      if (statusFilter === "planned" && raced) return false;
      return true;
    });
    if (sortBy === "start_at") {
      // Sort by start_at ascending; races without a start_at sink to
      // the bottom (their key becomes +Infinity).
      return [...filtered].sort((a, b) => {
        const ta = a.start_at ? new Date(a.start_at).getTime() : Infinity;
        const tb = b.start_at ? new Date(b.start_at).getTime() : Infinity;
        return ta - tb;
      });
    }
    // "newest" — preserve API order (created_at DESC). No re-sort.
    return filtered;
  }, [races, search, classFilter, statusFilter, sortBy]);

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
          <>
            <FilterBar
              search={search}
              onSearch={setSearch}
              classFilter={classFilter}
              onClassFilter={setClassFilter}
              statusFilter={statusFilter}
              onStatusFilter={setStatusFilter}
              sortBy={sortBy}
              onSortBy={setSortBy}
              availableClasses={availableClasses}
            />

            {filteredSorted && filteredSorted.length === 0 ? (
              <p style={styles.muted}>No races match the current filters.</p>
            ) : (
              <ul style={styles.list}>
                {filteredSorted.map((r) => (
                  <RaceCard
                    key={r.id}
                    race={r}
                    isShared={!!currentUid && r.user_id && r.user_id !== currentUid}
                    onOpen={() => onOpen(r)}
                    onEdit={() => onEdit(r.id)}
                    onViewStats={
                      // Show the Stats button when the race has at least
                      // one mark rounding recorded — i.e. it was raced.
                      // Races that were planned but never tracked don't
                      // get the entry (nothing to show).
                      isRaced(r) ? () => onViewStats?.(r.id) : null
                    }
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
          </>
        )}
      </main>
    </div>
  );
}

// True when the race has been sailed (has stats or at least one mark
// passage). Used by both the Raced-pill decision and the Stats-button
// gating so they can never disagree.
function isRaced(race) {
  return Boolean(
    race.stats_available ||
      (race.mark_passes && race.mark_passes.length > 0),
  );
}

// ── Filter / sort bar ─────────────────────────────────────────────────
function FilterBar({
  search, onSearch,
  classFilter, onClassFilter,
  statusFilter, onStatusFilter,
  sortBy, onSortBy,
  availableClasses,
}) {
  return (
    <div style={styles.filterBar}>
      <input
        type="search"
        placeholder="Search races…"
        value={search}
        onChange={(e) => onSearch(e.target.value)}
        style={styles.filterInput}
        aria-label="Search races by name"
      />
      <select
        value={classFilter}
        onChange={(e) => onClassFilter(e.target.value)}
        style={styles.filterSelect}
        aria-label="Filter by boat class"
      >
        <option value="all">All boats</option>
        {availableClasses.map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </select>
      <select
        value={statusFilter}
        onChange={(e) => onStatusFilter(e.target.value)}
        style={styles.filterSelect}
        aria-label="Filter by raced status"
      >
        <option value="all">All races</option>
        <option value="raced">Raced only</option>
        <option value="planned">Planned only</option>
      </select>
      <select
        value={sortBy}
        onChange={(e) => onSortBy(e.target.value)}
        style={styles.filterSelect}
        aria-label="Sort"
      >
        <option value="newest">Newest first</option>
        <option value="start_at">Start date</option>
      </select>
    </div>
  );
}

function RaceCard({ race, isShared, onOpen, onEdit, onViewStats, onDelete }) {
  // Card body click = the primary action (load on map). Edit and Delete
  // are explicit buttons in the action cluster on the right. Keyboard
  // users get the same primary action via Enter on the focused row.
  const onKey = (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onOpen();
    }
  };
  const raced = isRaced(race);
  return (
    <li style={styles.card}>
      <div
        style={styles.cardMain}
        onClick={onOpen}
        onKeyDown={onKey}
        role="button"
        tabIndex={0}
      >
        <h3 style={styles.cardName}>
          {race.name}
          {isShared && (
            <span style={{
              marginLeft: 8,
              fontSize: 10,
              letterSpacing: "0.06em",
              textTransform: "uppercase",
              color: "#1a4d8f",
              border: "1px solid #1a4d8f",
              borderRadius: 4,
              padding: "1px 6px",
              fontWeight: 600,
              verticalAlign: "middle",
            }}>
              Shared
            </span>
          )}
          {raced && (
            <span style={styles.racedPill}>Raced</span>
          )}
        </h3>
        {race.start_at && (
          <div style={styles.cardDate}>{formatRaceDate(race.start_at)}</div>
        )}
        <div style={styles.cardMeta}>
          <span style={styles.badge}>{race.mode}</span>
          <span style={styles.metaSep}>·</span>
          <span>{race.boat_class}</span>
          <span style={styles.metaSep}>·</span>
          <span>{race.marks.length} {race.marks.length === 1 ? "mark" : "marks"}</span>
        </div>
      </div>
      <div style={styles.cardActions}>
        <button onClick={onOpen} style={styles.openBtn}>Open on map</button>
        {onViewStats ? (
          <button onClick={onViewStats} style={styles.editBtn}>Stats</button>
        ) : null}
        <button onClick={onEdit} style={styles.editBtn}>Edit</button>
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
  openBtn: {
    border: "none",
    background: "var(--ink)",
    color: "var(--paper)",
    borderRadius: "var(--r-sm)",
    padding: "8px 14px",
    fontSize: 13,
    fontWeight: 500,
    cursor: "pointer",
    fontFamily: "inherit",
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

  // ── Added 2026-05-26 ──────────────────────────────────────────────
  filterBar: {
    display: "flex",
    flexWrap: "wrap",
    gap: 8,
    marginBottom: 16,
    padding: 12,
    border: "1px solid var(--rule)",
    borderRadius: "var(--r-md)",
    background: "var(--paper)",
  },
  filterInput: {
    flex: "1 1 220px",
    minWidth: 180,
    padding: "8px 12px",
    fontSize: 13,
    border: "1px solid var(--rule)",
    borderRadius: "var(--r-sm)",
    background: "var(--paper)",
    color: "var(--ink)",
    fontFamily: "inherit",
  },
  filterSelect: {
    padding: "8px 12px",
    fontSize: 13,
    border: "1px solid var(--rule)",
    borderRadius: "var(--r-sm)",
    background: "var(--paper)",
    color: "var(--ink)",
    cursor: "pointer",
    fontFamily: "inherit",
  },
  cardDate: {
    fontSize: 13,
    color: "var(--ink-3)",
    margin: "0 0 4px",
  },
  racedPill: {
    marginLeft: 8,
    fontSize: 10,
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    color: "#0f6e56",
    background: "#E1F5EE",
    borderRadius: 999,
    padding: "2px 10px",
    fontWeight: 600,
    verticalAlign: "middle",
  },
};
