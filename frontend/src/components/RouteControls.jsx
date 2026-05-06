// RouteControls - Compute Route button + status badge.
//
// Designed to slot into the active-race overlay alongside Record/Edit/✕.
// Keeps all routing UI logic in one file so MapView.jsx changes stay
// minimal: it just hands its `routing` object (from useRouting) down.
//
// Dark-glass theme: button uses the same frosted treatment as the
// other buttons in the race overlay (rgba white tint over the dark
// glass parent). Status text is light-on-dark; errors use a brighter
// red so they read against the dark surface.

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
      title="Compute optimal route based on current forecast"
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
    return <div style={styles.routeError}>{formatError(error)}</div>;
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

// Try to surface a clean message instead of dumping raw API JSON.
// The 425 path returns a structured detail object; everything else
// just shows the message as-is.
function formatError(err) {
  // err is whatever useRouting setError-ed; usually a string.
  if (typeof err !== "string") return "Route compute failed.";

  // Pattern from api.js: "API 425: <body text>"
  const m = err.match(/^API (\d+):\s*(.*)$/s);
  if (!m) return err;

  const [, code, body] = m;

  if (code === "425") {
    try {
      const parsed = JSON.parse(body);
      const detail = parsed?.detail?.detail || parsed?.detail || "";
      const availableAt = parsed?.detail?.available_at;
      const hoursUntil = parsed?.detail?.hours_until_available;

      if (availableAt && typeof hoursUntil === "number") {
        const hrs = hoursUntil >= 1
          ? `${Math.round(hoursUntil)}h`
          : `${Math.round(hoursUntil * 60)}m`;
        return `Forecast not yet available · ready in ${hrs}`;
      }
      if (detail) return `Forecast pending: ${detail}`;
    } catch {
      /* fall through */
    }
    return "Forecast not yet available for this race window.";
  }

  return `${code}: ${body.slice(0, 80)}${body.length > 80 ? "…" : ""}`;
}

const styles = {
  // Frosted-glass button matching the other dark-overlay buttons.
  routeBtn: {
    display: "flex",
    alignItems: "center",
    gap: 6,
    height: 44,
    minWidth: 110,
    padding: "0 14px",
    border: "1px solid rgba(255, 255, 255, 0.20)",
    background: "rgba(255, 255, 255, 0.10)",
    backdropFilter: "blur(16px) saturate(180%)",
    WebkitBackdropFilter: "blur(16px) saturate(180%)",
    borderRadius: "var(--r-md)",
    fontSize: 13,
    color: "var(--paper-ink)",
    cursor: "pointer",
    fontFamily: "inherit",
    fontWeight: 500,
    boxShadow: "0 1px 0 rgba(255, 255, 255, 0.10) inset",
    transition: "background 0.15s, border-color 0.15s, transform 0.08s",
  },
  // When a route is computed, hint with a magenta glow tied to the
  // route line on the map.
  routeBtnHasRoute: {
    border: "1px solid rgba(192, 38, 211, 0.60)",
    background: "rgba(192, 38, 211, 0.18)",
    color: "#f0a8ff",
  },
  routeIcon: {
    color: "#e879f9",
    fontSize: 11,
  },
  routeLabel: {
    fontVariantNumeric: "tabular-nums",
  },

  // Status text - light on dark.
  routeBadge: {
    fontSize: 11,
    color: "var(--paper-ink-2)",
    marginTop: 2,
    fontVariantNumeric: "tabular-nums",
  },
  // Brighter red for legibility on dark glass; soft so it doesn't
  // dominate the panel.
  routeError: {
    fontSize: 11,
    color: "#ff8a92",
    marginTop: 2,
    maxWidth: 260,
    lineHeight: 1.35,
  },
};
