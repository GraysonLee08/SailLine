// RaceStatsView - post-race summary screen.
//
// Reachable from:
//   * "View stats" entry on RacesListView for any completed race
//   * Auto-navigated to when useAutoStopRecorder fires stop() during a
//     live race (see AppView.jsx for the wiring)
//
// Layout (top → bottom):
//   1. Header bar with race name, date, boat class, Back button
//   2. Headline stat tiles (distance / elapsed / avg SOG / max SOG)
//   3. AI summary card (recap paragraph + tips list)
//      Shows a "generating…" skeleton while summary_pending=true
//      Pro users see a Regenerate button
//   4. Wind summary card (only when wind_snapshot exists)
//   5. Leg-by-leg table
//   6. Speed-over-time sparkline (inline SVG, no chart library)
//   7. Read-only map: course marks + recorded track polyline
//
// Pulls all backend data through useRaceStats. The map uses the new
// MapCanvas + MarksLayer + TrackLayer composition (see
// 2026-05-14_post-race-stats-multi-session-plan.md for the rationale).

import { useEffect, useMemo, useState } from "react";

import { useRaceStats } from "./hooks/useRaceStats";
import { MapCanvas } from "./components/MapCanvas.jsx";
import { MarksLayer } from "./components/layers/MarksLayer.jsx";
import { TrackLayer } from "./components/layers/TrackLayer.jsx";

export default function RaceStatsView({ raceId, onBack, tier = "free" }) {
  const { data, track, loading, error, regenerating, regenerate } =
    useRaceStats(raceId);

  if (!raceId) {
    return (
      <div style={styles.shell}>
        <Header title="No race selected" onBack={onBack} />
        <div style={styles.empty}>Pick a race from the list.</div>
      </div>
    );
  }

  if (loading && !data) {
    return (
      <div style={styles.shell}>
        <Header title="Loading…" onBack={onBack} />
        <div style={styles.empty}>Computing stats…</div>
      </div>
    );
  }

  if (error && !data) {
    return (
      <div style={styles.shell}>
        <Header title="Error" onBack={onBack} />
        <div style={styles.errorBlock}>{error}</div>
      </div>
    );
  }

  const stats = data?.stats || null;
  const summary = data?.ai_summary || null;
  const wind = data?.wind || null;
  const summaryPending = !!data?.summary_pending;
  const isPro = tier === "pro" || tier === "hardware";

  return (
    <div style={styles.shell}>
      <Header
        title={data?.name || "Race"}
        subtitle={subtitleFor(data)}
        onBack={onBack}
      />

      <div style={styles.scrollArea}>
        {stats ? (
          <StatTiles stats={stats} />
        ) : (
          <div style={styles.empty}>No track recorded for this race.</div>
        )}

        <SummaryCard
          summary={summary}
          pending={summaryPending}
          onRegenerate={isPro ? regenerate : null}
          regenerating={regenerating}
        />

        {wind ? <WindCard wind={wind} /> : null}

        {stats?.legs?.length ? <LegsTable legs={stats.legs} /> : null}

        {stats?.speed_series?.length ? (
          <SpeedChart series={stats.speed_series} />
        ) : null}

        <MapPanel marks={data?.marks || []} track={track || []} />
      </div>
    </div>
  );
}

// ─── Header ───────────────────────────────────────────────────────────


function Header({ title, subtitle, onBack }) {
  return (
    <div style={styles.header}>
      <button
        type="button"
        onClick={onBack}
        style={styles.backBtn}
        aria-label="Back"
      >
        ←
      </button>
      <div style={styles.headerText}>
        <div style={styles.headerTitle}>{title}</div>
        {subtitle ? <div style={styles.headerSub}>{subtitle}</div> : null}
      </div>
    </div>
  );
}

function subtitleFor(data) {
  if (!data) return null;
  const parts = [];
  if (data.boat_class) parts.push(data.boat_class);
  if (data.start_at) parts.push(formatDate(data.start_at));
  return parts.join(" · ");
}


// ─── Stat tiles ──────────────────────────────────────────────────────


function StatTiles({ stats }) {
  const tiles = [
    {
      label: "Distance",
      value: formatDistance(stats.distance_m),
    },
    {
      label: "Elapsed",
      value: formatDuration(stats.elapsed_s),
    },
    {
      label: "Avg SOG",
      value: `${stats.avg_sog_kt.toFixed(1)} kt`,
      sub: `moving ${stats.avg_moving_sog_kt.toFixed(1)} kt`,
    },
    {
      label: "Max SOG",
      value: `${stats.max_sog_kt.toFixed(1)} kt`,
    },
  ];
  return (
    <div style={styles.tilesRow}>
      {tiles.map((t) => (
        <div key={t.label} style={styles.tile}>
          <div style={styles.tileLabel}>{t.label}</div>
          <div style={styles.tileValue}>{t.value}</div>
          {t.sub ? <div style={styles.tileSub}>{t.sub}</div> : null}
        </div>
      ))}
    </div>
  );
}


// ─── AI summary card ─────────────────────────────────────────────────


function SummaryCard({ summary, pending, onRegenerate, regenerating }) {
  return (
    <section style={styles.card}>
      <div style={styles.cardHead}>
        <div style={styles.cardTitle}>Race recap</div>
        {onRegenerate ? (
          <button
            onClick={onRegenerate}
            disabled={regenerating}
            style={styles.regenerateBtn}
          >
            {regenerating ? "Working…" : "Regenerate"}
          </button>
        ) : null}
      </div>
      {summary ? (
        <>
          <p style={styles.recap}>{summary.recap}</p>
          {summary.tips?.length ? (
            <ul style={styles.tipsList}>
              {summary.tips.map((t, i) => (
                <li key={i} style={styles.tip}>{t}</li>
              ))}
            </ul>
          ) : null}
        </>
      ) : pending ? (
        <Skeleton lines={4} />
      ) : (
        <div style={styles.muted}>
          Summary not available. The AI service may be offline or you may
          not have a key configured.
        </div>
      )}
    </section>
  );
}

function Skeleton({ lines = 3 }) {
  return (
    <div style={styles.skeletonBlock}>
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          style={{
            ...styles.skeletonLine,
            width: `${70 + Math.round((i * 7) % 30)}%`,
          }}
        />
      ))}
      <div style={styles.skeletonNote}>Generating recap…</div>
    </div>
  );
}


// ─── Wind card ───────────────────────────────────────────────────────


function WindCard({ wind }) {
  if (wind.mean_speed_kt == null) {
    return (
      <section style={styles.card}>
        <div style={styles.cardTitle}>Wind</div>
        <div style={styles.muted}>
          Forecast wasn't available at the race location.
        </div>
      </section>
    );
  }
  return (
    <section style={styles.card}>
      <div style={styles.cardTitle}>Wind during the race</div>
      <div style={styles.windGrid}>
        <Datum label="Average" value={`${wind.mean_speed_kt.toFixed(1)} kt`} />
        <Datum label="Max" value={`${wind.max_speed_kt.toFixed(1)} kt`} />
        <Datum
          label="From"
          value={`${Math.round(wind.mean_dir_deg)}° ${cardinal(wind.mean_dir_deg)}`}
        />
        <Datum
          label="Direction range"
          value={`${Math.round(wind.dir_range_deg)}°`}
          sub={
            wind.dir_range_deg > 20
              ? "Noticeable shift across the race"
              : "Wind held steady"
          }
        />
      </div>
      {wind.cell_coverage != null && wind.cell_coverage < 0.5 ? (
        <div style={styles.muted}>
          Forecast covered {Math.round(wind.cell_coverage * 100)}% of the
          race area — wind context is approximate.
        </div>
      ) : null}
    </section>
  );
}

function Datum({ label, value, sub }) {
  return (
    <div style={styles.datum}>
      <div style={styles.tileLabel}>{label}</div>
      <div style={styles.datumValue}>{value}</div>
      {sub ? <div style={styles.tileSub}>{sub}</div> : null}
    </div>
  );
}


// ─── Legs table ──────────────────────────────────────────────────────


function LegsTable({ legs }) {
  return (
    <section style={styles.card}>
      <div style={styles.cardTitle}>Legs</div>
      <table style={styles.table}>
        <thead>
          <tr>
            <th style={styles.th}>Leg</th>
            <th style={styles.th}>From → To</th>
            <th style={{ ...styles.th, textAlign: "right" }}>Distance</th>
            <th style={{ ...styles.th, textAlign: "right" }}>Elapsed</th>
            <th style={{ ...styles.th, textAlign: "right" }}>Avg SOG</th>
          </tr>
        </thead>
        <tbody>
          {legs.map((leg) => (
            <tr key={leg.leg_index}>
              <td style={styles.td}>{leg.leg_index + 1}</td>
              <td style={styles.td}>
                {leg.from_label} → {leg.to_label}
              </td>
              <td style={{ ...styles.td, textAlign: "right" }}>
                {formatDistance(leg.distance_m)}
              </td>
              <td style={{ ...styles.td, textAlign: "right" }}>
                {formatDuration(leg.elapsed_s)}
              </td>
              <td style={{ ...styles.td, textAlign: "right" }}>
                {leg.avg_sog_kt.toFixed(1)} kt
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}


// ─── Speed chart (inline SVG, no chart lib) ──────────────────────────


function SpeedChart({ series }) {
  // Pad to keep the line clear of the chart edges.
  const W = 720;
  const H = 160;
  const P = { top: 12, right: 16, bottom: 24, left: 36 };
  const innerW = W - P.left - P.right;
  const innerH = H - P.top - P.bottom;

  const { tMax, sMax, sMin, path, gridY } = useMemo(() => {
    const tMax = series[series.length - 1].t_offset_s || 1;
    const sMax = Math.max(...series.map((p) => p.sog_kt), 1);
    const sMin = 0;
    const xOf = (t) => P.left + (t / tMax) * innerW;
    const yOf = (s) => P.top + (1 - (s - sMin) / (sMax - sMin || 1)) * innerH;
    const d = series
      .map((p, i) => `${i === 0 ? "M" : "L"}${xOf(p.t_offset_s).toFixed(1)} ${yOf(p.sog_kt).toFixed(1)}`)
      .join(" ");
    // Gridlines every ~25% of max
    const ticks = 4;
    const gridY = Array.from({ length: ticks + 1 }, (_, i) => {
      const s = sMin + (i / ticks) * (sMax - sMin);
      return { s, y: yOf(s) };
    });
    return { tMax, sMax, sMin, path: d, gridY };
  }, [series, innerH, innerW, P.left, P.top]);

  return (
    <section style={styles.card}>
      <div style={styles.cardTitle}>Speed over time</div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        style={{ display: "block", maxWidth: "100%" }}
        role="img"
        aria-label="Speed over the race"
      >
        {gridY.map((g, i) => (
          <g key={i}>
            <line
              x1={P.left}
              x2={W - P.right}
              y1={g.y}
              y2={g.y}
              stroke="#e5e5ea"
              strokeWidth="1"
            />
            <text
              x={P.left - 6}
              y={g.y + 4}
              fontSize="10"
              fill="#8e8e93"
              textAnchor="end"
              fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
            >
              {g.s.toFixed(0)}
            </text>
          </g>
        ))}
        <path d={path} fill="none" stroke="#1a73e8" strokeWidth="2" />
        <text
          x={W - P.right}
          y={H - 6}
          fontSize="10"
          fill="#8e8e93"
          textAnchor="end"
          fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
        >
          {formatDuration(tMax)} →
        </text>
        <text
          x={P.left}
          y={H - 6}
          fontSize="10"
          fill="#8e8e93"
          fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
        >
          0
        </text>
      </svg>
    </section>
  );
}


// ─── Map panel (read-only) ───────────────────────────────────────────


function MapPanel({ marks, track }) {
  const initialCenter = useMemo(() => {
    if (marks.length) {
      const lon = marks.reduce((a, m) => a + m.lon, 0) / marks.length;
      const lat = marks.reduce((a, m) => a + m.lat, 0) / marks.length;
      return [lon, lat];
    }
    if (track.length) return [track[0].lon, track[0].lat];
    return [-87.65, 42.05];
  }, [marks, track]);

  // Force a fresh map mount whenever the race id (encoded into marks/track)
  // changes so the initial center re-applies.
  const mountKey = useMemo(() => {
    if (marks.length) return `m:${marks[0].lat},${marks[0].lon}:${marks.length}`;
    if (track.length) return `t:${track[0].lat},${track[0].lon}:${track.length}`;
    return "empty";
  }, [marks, track]);

  return (
    <section style={styles.card}>
      <div style={styles.cardTitle}>Track</div>
      <div style={styles.mapBox}>
        <MapCanvas
          key={mountKey}
          initialCenter={initialCenter}
          initialZoom={12}
          interactive
          containerStyle={{ position: "absolute", inset: 0 }}
        >
          <MarksLayer marks={marks} fitOnMount={marks.length > 0} />
          <TrackLayer points={track} showEndpoints />
        </MapCanvas>
      </div>
    </section>
  );
}


// ─── Helpers ─────────────────────────────────────────────────────────


function formatDistance(meters) {
  if (meters == null) return "—";
  const nm = meters / 1852.0;
  if (nm < 1) return `${Math.round(meters)} m`;
  return `${nm.toFixed(2)} nm`;
}

function formatDuration(seconds) {
  if (seconds == null) return "—";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s - h * 3600) / 60);
  const sec = s - h * 3600 - m * 60;
  if (h > 0) return `${h}h ${m.toString().padStart(2, "0")}m`;
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

function formatDate(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

function cardinal(deg) {
  if (deg == null) return "";
  const pts = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
  const idx = Math.round(((deg % 360) / 45)) % 8;
  return pts[idx];
}


// ─── Styles ──────────────────────────────────────────────────────────


const styles = {
  shell: {
    position: "absolute",
    inset: 0,
    display: "flex",
    flexDirection: "column",
    background: "var(--paper, #f8f8f7)",
    color: "var(--paper-ink, #16161a)",
    fontFamily: "var(--sans, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif)",
  },
  header: {
    display: "flex",
    alignItems: "center",
    gap: 12,
    padding: "16px 18px",
    borderBottom: "1px solid var(--paper-line, #eaeaea)",
    flexShrink: 0,
  },
  backBtn: {
    width: 36,
    height: 36,
    borderRadius: 8,
    border: "1px solid var(--paper-line, #e5e5ea)",
    background: "white",
    fontSize: 18,
    cursor: "pointer",
  },
  headerText: { display: "flex", flexDirection: "column", gap: 2 },
  headerTitle: { fontSize: 18, fontWeight: 600 },
  headerSub: {
    fontSize: 12,
    color: "var(--paper-ink-3, #6a6a6f)",
    fontFamily: "var(--mono, ui-monospace, monospace)",
  },
  scrollArea: {
    flex: 1,
    minHeight: 0,
    overflow: "auto",
    padding: "16px 18px 40px",
    display: "flex",
    flexDirection: "column",
    gap: 14,
  },
  empty: {
    padding: 28,
    color: "var(--paper-ink-3, #6a6a6f)",
    textAlign: "center",
  },
  errorBlock: {
    padding: 16,
    margin: 18,
    border: "1px solid #f0c4c4",
    background: "#fdecec",
    color: "#8a1f1f",
    borderRadius: 8,
  },

  tilesRow: {
    display: "grid",
    gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
    gap: 10,
  },
  tile: {
    background: "white",
    borderRadius: 10,
    border: "1px solid var(--paper-line, #eaeaea)",
    padding: "12px 14px",
    minHeight: 76,
    display: "flex",
    flexDirection: "column",
    gap: 4,
  },
  tileLabel: {
    fontSize: 11,
    letterSpacing: "0.06em",
    textTransform: "uppercase",
    color: "var(--paper-ink-3, #6a6a6f)",
    fontWeight: 500,
  },
  tileValue: {
    fontSize: 22,
    fontWeight: 600,
    fontFamily: "var(--mono, ui-monospace, monospace)",
    fontVariantNumeric: "tabular-nums",
  },
  tileSub: {
    fontSize: 11,
    color: "var(--paper-ink-3, #6a6a6f)",
    fontFamily: "var(--mono, ui-monospace, monospace)",
  },

  card: {
    background: "white",
    borderRadius: 12,
    border: "1px solid var(--paper-line, #eaeaea)",
    padding: "14px 16px 16px",
  },
  cardHead: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 8,
  },
  cardTitle: {
    fontSize: 13,
    letterSpacing: "0.05em",
    textTransform: "uppercase",
    color: "var(--paper-ink-2, #3a3a40)",
    fontWeight: 600,
    marginBottom: 10,
  },
  regenerateBtn: {
    padding: "5px 10px",
    fontSize: 12,
    border: "1px solid var(--paper-line, #d8d8de)",
    background: "white",
    borderRadius: 6,
    cursor: "pointer",
    fontFamily: "inherit",
  },
  recap: {
    fontSize: 14,
    lineHeight: 1.55,
    whiteSpace: "pre-wrap",
    margin: 0,
  },
  tipsList: {
    margin: "12px 0 0",
    paddingLeft: 22,
    display: "flex",
    flexDirection: "column",
    gap: 6,
  },
  tip: { fontSize: 13, lineHeight: 1.45 },

  muted: {
    fontSize: 13,
    color: "var(--paper-ink-3, #6a6a6f)",
    marginTop: 6,
  },

  skeletonBlock: { display: "flex", flexDirection: "column", gap: 8 },
  skeletonLine: {
    height: 12,
    borderRadius: 4,
    background: "linear-gradient(90deg, #ececec, #f7f7f7, #ececec)",
    backgroundSize: "200% 100%",
    animation: "stats-shimmer 1.4s linear infinite",
  },
  skeletonNote: {
    fontSize: 11,
    color: "var(--paper-ink-3, #6a6a6f)",
    marginTop: 6,
    fontFamily: "var(--mono, ui-monospace, monospace)",
  },

  windGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
    gap: 12,
    marginBottom: 8,
  },
  datum: { display: "flex", flexDirection: "column", gap: 4 },
  datumValue: {
    fontSize: 18,
    fontWeight: 600,
    fontFamily: "var(--mono, ui-monospace, monospace)",
    fontVariantNumeric: "tabular-nums",
  },

  table: {
    width: "100%",
    borderCollapse: "collapse",
    fontVariantNumeric: "tabular-nums",
  },
  th: {
    textAlign: "left",
    fontSize: 11,
    letterSpacing: "0.05em",
    textTransform: "uppercase",
    color: "var(--paper-ink-3, #6a6a6f)",
    padding: "6px 8px",
    borderBottom: "1px solid var(--paper-line, #eaeaea)",
    fontWeight: 500,
  },
  td: {
    fontSize: 13,
    padding: "8px",
    borderBottom: "1px solid var(--paper-line, #f3f3f3)",
    fontFamily: "var(--mono, ui-monospace, monospace)",
  },

  mapBox: {
    position: "relative",
    width: "100%",
    height: 360,
    borderRadius: 8,
    overflow: "hidden",
    border: "1px solid var(--paper-line, #eaeaea)",
  },
};
