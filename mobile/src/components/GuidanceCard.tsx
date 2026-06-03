// GuidanceCard.tsx — live "next mark" guidance during recording.
//
// The single most valuable race-day UX surface on the phone: a glanceable
// card that always answers "what mark am I going to, how far, what
// bearing, am I laying it." Designed to be readable at arm's length in
// bright sunlight — large tabular numerals, high contrast, no decorative
// chrome.
//
// Sits in the lower-third of the recorder screen, BETWEEN the map and
// the Stop button. Anchored bottom so the sailor's thumb naturally
// rests above it (Stop) and below it (sheet handle).
//
// Empty state ("waiting for fix"): renders the same card chrome but
// with em-dashes in place of values, so the layout doesn't shift the
// first time a point arrives.

import { StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";

import { useTheme } from "../theme/ThemeProvider";

type GuidanceLike = {
  nextMark: { name?: string; lat: number; lon: number };
  nextMarkIndex: number;
  distanceM: number;
  bearingDeg: number;
  crossTrackM: number;
  fromMark: { lat: number; lon: number } | null;
} | null;

type Props = {
  guidance: GuidanceLike;
  /** Total marks in the course, for "Mark 2 of 4" labelling. */
  totalMarks: number;
  /** Boat's last-known speed (kt), or null if unavailable. */
  speedKt: number | null;
  /** Boat's last-known heading (deg true), or null. */
  headingDeg: number | null;
  /**
   * True when the recorder is live but hasn't received a fix with
   * speed/heading yet. Drives the "Waiting…" labels instead of plain
   * em-dashes so the user knows the values are coming, not broken.
   * 2026-06-03 A4. */
  awaitingGps?: boolean;
};

const METRES_PER_NM = 1852;

export function GuidanceCard({
  guidance,
  totalMarks,
  speedKt,
  headingDeg,
  awaitingGps = false,
}: Props) {
  const { colors, font, size, tabularVariant } = useTheme();

  // Bearing-arrow rotation: rotate an upward arrow by (bearing - heading)
  // so it points to the next mark RELATIVE to where the boat is heading.
  // When heading is unknown, we rotate by absolute bearing — still useful
  // (north-up reference).
  const arrowAngle =
    guidance && headingDeg != null
      ? guidance.bearingDeg - headingDeg
      : guidance?.bearingDeg ?? 0;

  const nextLabel = guidance
    ? guidance.nextMark.name && guidance.nextMark.name.trim().length > 0
      ? guidance.nextMark.name
      : `Mark ${guidance.nextMarkIndex + 1}`
    : "—";

  const ofTotal = totalMarks > 0 ? `of ${totalMarks}` : "";
  const distNm = guidance ? guidance.distanceM / METRES_PER_NM : null;

  // Cross-track sign → text. ±15m we treat as "on the line" — finer than
  // that is GPS noise and a flapping label is distracting.
  const xt = guidance?.crossTrackM ?? 0;
  const xtLabel =
    !guidance || guidance.fromMark == null || Math.abs(xt) < 15
      ? "ON LINE"
      : xt > 0
        ? `${Math.round(Math.abs(xt))}m RIGHT`
        : `${Math.round(Math.abs(xt))}m LEFT`;

  return (
    <View
      style={[
        styles.card,
        {
          backgroundColor: colors.surface.elevated,
          borderColor: colors.border.hairline,
          shadowColor: colors.scrim.shadow,
        },
      ]}
    >
      {/* Top row: bearing arrow + next mark name + index */}
      <View style={styles.row}>
        <View
          style={[
            styles.arrowWrap,
            { backgroundColor: `${colors.accent.primary}22` },
          ]}
        >
          <View style={{ transform: [{ rotate: `${arrowAngle}deg` }] }}>
            <Ionicons name="arrow-up" size={28} color={colors.accent.primary} />
          </View>
        </View>
        <View style={{ flex: 1 }}>
          <Text
            style={{
              color: colors.text.muted,
              fontFamily: font.body,
              fontSize: size.caption,
              letterSpacing: 0.8,
              textTransform: "uppercase",
            }}
          >
            Next mark {guidance ? ofTotal : ""}
          </Text>
          <Text
            style={{
              color: colors.text.primary,
              fontFamily: font.displaySemibold,
              fontSize: size.title,
              letterSpacing: -0.4,
              marginTop: 2,
            }}
            numberOfLines={1}
          >
            {nextLabel}
          </Text>
        </View>
      </View>

      {/* Bottom row: distance + speed + cross-track.
          When `awaitingGps` we replace bare em-dashes with a "GPS…"
          microcopy so the user knows the values are pending a fix
          rather than missing. 2026-06-03 A4. */}
      <View style={styles.statsRow}>
        <Stat
          label="Distance"
          value={
            distNm != null
              ? distNm < 0.5
                ? `${Math.round(distNm * METRES_PER_NM)}m`
                : `${distNm.toFixed(2)} nm`
              : awaitingGps
                ? "GPS…"
                : "—"
          }
          compact={awaitingGps && distNm == null}
        />
        <Stat
          label="Speed"
          value={
            speedKt != null
              ? `${speedKt.toFixed(1)} kt`
              : awaitingGps
                ? "GPS…"
                : "—"
          }
          compact={awaitingGps && speedKt == null}
        />
        <Stat
          label="Track"
          value={
            awaitingGps && (!guidance || guidance.fromMark == null)
              ? "GPS…"
              : xtLabel
          }
          compact
        />
      </View>
    </View>
  );
}

function Stat({
  label,
  value,
  compact = false,
}: {
  label: string;
  value: string;
  compact?: boolean;
}) {
  const { colors, font, size, tabularVariant } = useTheme();
  return (
    <View style={styles.stat}>
      <Text
        style={{
          color: colors.text.muted,
          fontFamily: font.body,
          fontSize: size.caption,
          letterSpacing: 0.6,
          textTransform: "uppercase",
        }}
      >
        {label}
      </Text>
      <Text
        style={[
          {
            color: colors.text.primary,
            fontFamily: font.tabularBold,
            fontSize: compact ? size.body : size.title,
            letterSpacing: -0.4,
            marginTop: 2,
          },
          tabularVariant,
        ]}
        numberOfLines={1}
      >
        {value}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    borderRadius: 18,
    borderWidth: StyleSheet.hairlineWidth,
    padding: 16,
    gap: 14,
    shadowOffset: { width: 0, height: 6 },
    shadowOpacity: 1,
    shadowRadius: 14,
    elevation: 6,
  },
  row: { flexDirection: "row", alignItems: "center", gap: 14 },
  arrowWrap: {
    width: 56,
    height: 56,
    borderRadius: 28,
    alignItems: "center",
    justifyContent: "center",
  },
  statsRow: { flexDirection: "row", gap: 14 },
  stat: { flex: 1 },
});
