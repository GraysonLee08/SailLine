// RouteControls — Compute Route button + status badge.
//
// Designed to slot into the active-race overlay alongside Record/Edit/✕.
// Keeps all routing UI logic in one file so MapView.jsx changes stay
// minimal: it just hands its `routing` object (from useRouting) down.
//
// Visual layout: button row + (when result is available) a small badge
// with total time and tack count below the buttons. Error and loading
// states surface inline.

export function ComputeRouteButton({ loading, onClick, hasRoute }) {
  return (
    <button
      onClick={onClick}
      disabled={loading}
      style={{
        ...styles.routeBtn,
        ...(hasRoute ? styles.routeBtnHasRoute : null),
        opacity: loading ? 0.6 : 1,
      }}
      aria-label={loading ? "Computing route" : "Compute optimal route"}
      title="Compute optimal route based on current HRRR wind"
    >
      <span style={styles.routeIcon} aria-hidden>
        ◆
      </span>
      <span style={styles.routeLabel}>
        {loading ? "Computing…" : hasRoute ? "Recompute" : "Compute Route"}
      </span>
    </button>
  );
}

export function RouteStatus({ meta, error }) {
  if (error) {
    return <div style={styles.routeError}>Route: {error}</div>;
  }
  if (!meta) return null;

  const hours = Math.floor(meta.total_minutes / 60);
  const mins = Math.round(meta.total_minutes - hours * 60);
  const timeLabel = hours > 0 ? `${hours}h ${mins}m` : `${mins}m`;
  const tackLabel =
    meta.tack_count === 0
      ? "no tacks"
      : `${meta.tack_count} tack${meta.tack_count === 1 ? "" : "s"}`;
  const reachLabel = meta.reached ? "" : " (closest approach)";
  const cacheLabel = meta.cached ? " · cached" : "";

  return (
    <div style={styles.routeBadge}>
      {timeLabel} · {tackLabel}{reachLabel}{cacheLabel}
    </div>
  );
}

const styles = {
  routeBtn: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    height: 44,
    minWidth: 110,
    padding: "0 14px",
    border: "1px solid var(--rule)",
    background: "var(--paper)",
    borderRadius: "var(--r-sm)",
    fontSize: 13,
    color: "var(--ink)",
    cursor: "pointer",
    fontFamily: "inherit",
    fontWeight: 500,
  },
  routeBtnHasRoute: {
    borderColor: "#c026d3", // magenta-ish to match the route line
    color: "#86198f",
  },
  routeIcon: {
    color: "#c026d3",
    fontSize: 11,
  },
  routeLabel: {
    fontVariantNumeric: "tabular-nums",
  },
  routeBadge: {
    fontSize: 11,
    color: "var(--ink-3)",
    marginTop: 2,
    fontVariantNumeric: "tabular-nums",
  },
  routeError: {
    fontSize: 11,
    color: "#b00020",
    marginTop: 2,
  },
};
