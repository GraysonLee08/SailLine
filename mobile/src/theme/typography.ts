// theme/typography.ts — font tokens.
//
// Two faces, picked deliberately (not Inter-everywhere AI slop):
//
//   * Inter for body — small-size legibility wins on a phone outdoors.
//   * Space Grotesk for display + tabular numerals — distinct letterforms,
//     monospace digit option ("ss01") that gives our speed / distance /
//     ETA readouts a clean racing-instrument feel without looking like
//     console output.
//
// `tabular` is a separate token for stats — same family as `display`
// but with the numeric variant. Components opt in by name to keep
// non-stat copy in proportional digits.

import { Platform } from "react-native";
import type { TextStyle } from "react-native";

export const FONT_FAMILIES = {
  /** Body copy, labels, sentence-case UI text. */
  body: "Inter_400Regular",
  bodyMedium: "Inter_500Medium",
  bodySemibold: "Inter_600SemiBold",
  bodyBold: "Inter_700Bold",
  /** Display headings, sheet titles, race name. */
  display: "SpaceGrotesk_500Medium",
  displaySemibold: "SpaceGrotesk_600SemiBold",
  displayBold: "SpaceGrotesk_700Bold",
  /** Numeric readouts (speed, distance, ETA, countdowns). */
  tabular: "SpaceGrotesk_500Medium",
  tabularBold: "SpaceGrotesk_700Bold",
} as const;

export const FONT_SIZES = {
  caption: 11,
  small: 12,
  body: 14,
  bodyLg: 16,
  subtitle: 17,
  title: 22,
  display: 28,
  hero: 40,
  /** Used for the live recording speed/heading readouts. */
  stat: 32,
} as const;

export const LINE_HEIGHTS = {
  tight: 1.15,
  snug: 1.3,
  normal: 1.45,
} as const;

/**
 * iOS exposes a `fontVariant: ['tabular-nums']` style prop that forces
 * monospaced digits without changing the font. We layer this on top of
 * `tabular` family so values still line up perfectly when the Google
 * font hasn't finished loading and we fall back to the system face.
 */
export const TABULAR_FONT_VARIANT: TextStyle =
  Platform.OS === "ios"
    ? { fontVariant: ["tabular-nums"] }
    : {};
