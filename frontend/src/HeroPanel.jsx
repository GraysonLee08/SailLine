// HeroPanel — cinematic dark left panel.
//
// Three layers, top to bottom in z-order:
//   1. Deep gradient background (night → night-2)
//   2. Animated wind streamlines (subtle SVG, evokes the product
//      without resorting to a screenshot or marketing photo)
//   3. Foreground content: wordmark top, big display headline center,
//      live system status grid bottom.
//
// Mobile (< 900px): the entire panel collapses out and only the form
// shows. The wordmark migrates into the form panel via a media query
// applied inline at runtime (see useEffect at bottom of the file).

import { useEffect, useState } from "react";

export default function HeroPanel() {
  // Wind direction for the live data accent. Random-feels-real.
  // We re-roll every 4s so the panel breathes a little — same energy
  // as a Bloomberg ticker, just slower.
  const [wind] = useTickingWind();

  return (
    <aside style={styles.panel} className="sailline-hero-panel">
      <Streamlines />

      <div style={styles.layer}>
        {/* Top: wordmark */}
        <div style={styles.top}>
          <div className="wordmark wordmark--inverse">
            SailLine<span className="dot">.</span>
          </div>
          <span className="t-label t-label--inverse" style={{ letterSpacing: "0.18em" }}>
            v1.0 · Beta
          </span>
        </div>

        {/* Middle: headline */}
        <div style={styles.middle}>
          <p className="t-label t-label--inverse" style={{ marginBottom: 20 }}>
            Race intelligence for sailors
          </p>
          <h1 className="t-display" style={styles.headline}>
            21 forecasts.
            <br />
            <span style={{ color: "rgba(255,255,255,0.55)" }}>One optimal route.</span>
          </h1>
          <p style={styles.subhead}>
            Probabilistic ensemble routing, ML-enhanced polars, and an AI tactical advisor — built
            for Great Lakes race crews.
          </p>
        </div>

        {/* Bottom: live status grid — the "intelligent" data accent */}
        <div style={styles.statusGrid}>
          <StatusCell label="Wind" value={`${wind.speed.toFixed(1)} kt`} sub={`${wind.dir}°`} />
          <StatusCell label="Models" value="21" sub="GEFS members" />
          <StatusCell label="Recalc" value="2 min" sub="in-race cadence" />
          <StatusCell label="Region" value="Great Lakes" sub="MORF · Mac" live />
        </div>
      </div>

      {/* Inline media query — collapses the hero on narrow viewports.
          Keeps the file self-contained without a separate CSS file. */}
      <ResponsiveStyles />
    </aside>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Streamlines — subtle animated wind lines drawn in SVG.              */
/* ─────────────────────────────────────────────────────────────────── */

function Streamlines() {
  // Static, hand-positioned curves. Each one animates left-to-right
  // via a CSS keyframe. The randomness in delay/duration creates the
  // organic feel of wind without us having to compute fluid dynamics.
  const lines = [
    { y: 80, len: 320, opacity: 0.18, dur: 14, delay: 0 },
    { y: 160, len: 240, opacity: 0.12, dur: 18, delay: 2 },
    { y: 240, len: 380, opacity: 0.22, dur: 12, delay: 4 },
    { y: 340, len: 280, opacity: 0.14, dur: 16, delay: 1 },
    { y: 440, len: 360, opacity: 0.2, dur: 13, delay: 3 },
    { y: 540, len: 220, opacity: 0.1, dur: 20, delay: 5 },
    { y: 640, len: 320, opacity: 0.16, dur: 15, delay: 2.5 },
    { y: 740, len: 280, opacity: 0.12, dur: 17, delay: 6 },
  ];

  return (
    <>
      <style>{`
        @keyframes sailline-wind {
          0%   { transform: translateX(-40%); opacity: 0; }
          15%  { opacity: var(--line-opacity); }
          85%  { opacity: var(--line-opacity); }
          100% { transform: translateX(120%); opacity: 0; }
        }
        .sailline-streamline {
          animation-name: sailline-wind;
          animation-iteration-count: infinite;
          animation-timing-function: linear;
        }
      `}</style>
      <svg
        viewBox="0 0 800 900"
        preserveAspectRatio="xMidYMid slice"
        style={styles.streamlines}
        aria-hidden="true"
      >
        {/* Faint compass ring as a static anchor */}
        <circle
          cx="400"
          cy="450"
          r="280"
          fill="none"
          stroke="rgba(255,255,255,0.04)"
          strokeWidth="1"
        />
        <circle
          cx="400"
          cy="450"
          r="180"
          fill="none"
          stroke="rgba(255,255,255,0.05)"
          strokeWidth="1"
          strokeDasharray="2 6"
        />

        {lines.map((l, i) => (
          <path
            key={i}
            className="sailline-streamline"
            d={`M -${l.len} ${l.y} Q ${l.len / 2} ${l.y - 12}, ${l.len} ${l.y}`}
            stroke="rgba(255,255,255,0.5)"
            strokeWidth="1"
            fill="none"
            style={{
              "--line-opacity": l.opacity,
              animationDuration: `${l.dur}s`,
              animationDelay: `${l.delay}s`,
            }}
          />
        ))}
      </svg>
    </>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* StatusCell — one cell of the live status grid                       */
/* ─────────────────────────────────────────────────────────────────── */

function StatusCell({ label, value, sub, live }) {
  return (
    <div style={styles.statusCell}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
        <span className="t-label t-label--inverse">{label}</span>
        {live && <span style={styles.liveDot} />}
      </div>
      <div className="t-mono" style={styles.statusValue}>
        {value}
      </div>
      <div className="t-mono" style={styles.statusSub}>
        {sub}
      </div>
    </div>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* useTickingWind — fake-but-realistic wind data for the hero          */
/* ─────────────────────────────────────────────────────────────────── */

function useTickingWind() {
  const [wind, setWind] = useState({ speed: 14.2, dir: 247 });

  useEffect(() => {
    const id = setInterval(() => {
      setWind((w) => ({
        speed: clamp(w.speed + (Math.random() - 0.5) * 0.6, 8, 22),
        dir: ((w.dir + Math.round((Math.random() - 0.5) * 8)) + 360) % 360,
      }));
    }, 4000);
    return () => clearInterval(id);
  }, []);

  return [wind];
}

function clamp(n, lo, hi) {
  return Math.max(lo, Math.min(hi, n));
}

/* ─────────────────────────────────────────────────────────────────── */
/* ResponsiveStyles — emit a <style> tag for the media query           */
/* ─────────────────────────────────────────────────────────────────── */

function ResponsiveStyles() {
  return (
    <style>{`
      @media (max-width: 900px) {
        .sailline-hero-panel { display: none !important; }
      }
    `}</style>
  );
}

/* ─────────────────────────────────────────────────────────────────── */
/* Styles                                                              */
/* ─────────────────────────────────────────────────────────────────── */

const styles = {
  panel: {
    position: "relative",
    overflow: "hidden",
    background: "linear-gradient(155deg, #0a0e14 0%, #111723 60%, #1a2332 100%)",
    color: "var(--paper-ink)",
    minHeight: "100vh",
  },
  streamlines: {
    position: "absolute",
    inset: 0,
    width: "100%",
    height: "100%",
    pointerEvents: "none",
  },
  layer: {
    position: "relative",
    zIndex: 1,
    height: "100%",
    minHeight: "100vh",
    padding: "48px 56px",
    display: "flex",
    flexDirection: "column",
    justifyContent: "space-between",
    boxSizing: "border-box",
  },
  top: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
  },
  middle: {
    paddingTop: 60,
    paddingBottom: 40,
  },
  headline: {
    fontSize: "clamp(48px, 6vw, 84px)",
    fontWeight: 700,
    letterSpacing: "-0.035em",
    lineHeight: 0.98,
    margin: 0,
  },
  subhead: {
    fontSize: 17,
    lineHeight: 1.55,
    color: "var(--paper-ink-2)",
    maxWidth: 480,
    marginTop: 28,
    marginBottom: 0,
  },
  statusGrid: {
    display: "grid",
    gridTemplateColumns: "repeat(4, 1fr)",
    gap: 24,
    paddingTop: 32,
    borderTop: "1px solid rgba(255, 255, 255, 0.08)",
  },
  statusCell: {
    display: "flex",
    flexDirection: "column",
  },
  statusValue: {
    fontSize: 22,
    fontWeight: 500,
    color: "var(--paper-ink)",
    lineHeight: 1.1,
  },
  statusSub: {
    fontSize: 11,
    color: "var(--paper-ink-3)",
    marginTop: 4,
    letterSpacing: "0.02em",
  },
  liveDot: {
    width: 6,
    height: 6,
    borderRadius: "50%",
    background: "var(--accent)",
    boxShadow: "0 0 0 4px rgba(255, 91, 31, 0.18)",
  },
};
