// theme/colors.ts — light + dark palettes, semantic tokens.
//
// Light is the default: on-water glare makes dark themes hard to read in
// the cockpit. Dark theme is opt-in for dusk/night sailing and the system-
// follow mode.
//
// Token model: semantic names, not raw colour names. Components read
// `colors.surface.elevated` (not `colors.zinc[50]`) so swapping a token
// across themes flips every consumer at once. Mapbox-adjacent tokens
// (`map.routeStroke`, `map.markFill`, etc.) live in the same record so
// the entire map look swaps with the theme.
//
// Sources of the palette:
//   - Cool water blues drawn from Lake Michigan satellite at noon —
//     not generic Material blue.
//   - Sharp orange `accent.route` for the routing polyline and "faster
//     route" alerts, picked for max separation from blue on a satellite
//     basemap (complementary on the colour wheel, distinguishable for
//     deuteranopia).

export type ThemeMode = "light" | "dark";

export type Palette = {
  /** Map-related colours, used by Mapbox layer styles + overlays. */
  map: {
    /** Background behind the map while tiles load. */
    background: string;
    routeStroke: string;
    routeCasing: string;
    markFill: string;
    markStroke: string;
    markLabel: string;
    userHeading: string;
    userPosition: string;
    userPositionRing: string;
  };
  /** Surfaces drawn over the map: sheets, cards, FABs. */
  surface: {
    /** The bottom-sheet body. */
    sheet: string;
    /** Cards/list rows inside the sheet. */
    elevated: string;
    /** Floating buttons over the map. */
    floating: string;
    /** Sign-in screen and other non-map-backed full pages. */
    page: string;
  };
  text: {
    primary: string;
    secondary: string;
    muted: string;
    inverse: string;
    onAccent: string;
  };
  border: {
    /** Hairline used on sheets, cards, FAB outlines. */
    hairline: string;
    /** Slightly stronger separator. */
    divider: string;
  };
  accent: {
    /** Primary CTAs. */
    primary: string;
    primaryPressed: string;
    /** Routing polyline + better-route banner. */
    route: string;
    /** Recording (live, recording-in-progress) state. */
    recording: string;
    /** Stop button. */
    stop: string;
    /** Caution / warning banners. */
    warning: string;
    /** Soft success — "raced" pill, OK toasts. */
    success: string;
  };
  /** Translucent overlays for shadows, scrim, sheet handles. */
  scrim: {
    handle: string;
    overlay: string;
    shadow: string;
  };
};

const LIGHT: Palette = {
  map: {
    background: "#dbe7ec",
    routeStroke: "#f56b2a",
    routeCasing: "#ffffff",
    markFill: "#0b2a3a",
    markStroke: "#ffffff",
    markLabel: "#0b2a3a",
    userHeading: "#0d6efd",
    userPosition: "#0d6efd",
    userPositionRing: "rgba(13, 110, 253, 0.18)",
  },
  surface: {
    sheet: "#ffffff",
    elevated: "#f6f8fa",
    floating: "#ffffff",
    page: "#f3f6f8",
  },
  text: {
    primary: "#0b2a3a",
    secondary: "#3a5b6c",
    muted: "#7a93a0",
    inverse: "#ffffff",
    onAccent: "#ffffff",
  },
  border: {
    hairline: "rgba(11, 42, 58, 0.08)",
    divider: "rgba(11, 42, 58, 0.14)",
  },
  accent: {
    primary: "#0d6efd",
    primaryPressed: "#0a58c8",
    route: "#f56b2a",
    recording: "#e63946",
    stop: "#c0392b",
    warning: "#ef9f27",
    success: "#22a06b",
  },
  scrim: {
    handle: "rgba(11, 42, 58, 0.22)",
    overlay: "rgba(0, 0, 0, 0.35)",
    shadow: "rgba(11, 42, 58, 0.18)",
  },
};

const DARK: Palette = {
  map: {
    background: "#06121a",
    routeStroke: "#ff8a5b",
    routeCasing: "#0b2a3a",
    markFill: "#f5f7fa",
    markStroke: "#0b2a3a",
    markLabel: "#f5f7fa",
    userHeading: "#5dadff",
    userPosition: "#5dadff",
    userPositionRing: "rgba(93, 173, 255, 0.22)",
  },
  surface: {
    sheet: "#0f2230",
    elevated: "#13303f",
    floating: "#13303f",
    page: "#0b1f2a",
  },
  text: {
    primary: "#f5f7fa",
    secondary: "#a9c3d0",
    muted: "#6b8694",
    inverse: "#0b2a3a",
    onAccent: "#ffffff",
  },
  border: {
    hairline: "rgba(245, 247, 250, 0.08)",
    divider: "rgba(245, 247, 250, 0.14)",
  },
  accent: {
    primary: "#5dadff",
    primaryPressed: "#3a8de0",
    route: "#ff8a5b",
    recording: "#ef6a78",
    stop: "#d6634f",
    warning: "#f4b860",
    success: "#5dcaa5",
  },
  scrim: {
    handle: "rgba(245, 247, 250, 0.22)",
    overlay: "rgba(0, 0, 0, 0.55)",
    shadow: "rgba(0, 0, 0, 0.45)",
  },
};

export const PALETTES: Record<ThemeMode, Palette> = {
  light: LIGHT,
  dark: DARK,
};
