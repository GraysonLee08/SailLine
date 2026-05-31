// MarkPassControls.tsx — horizontal scrollable row of mark pills shown
// during a race. Each mark is always tappable so the sailor can confirm
// any pass the auto-detector missed.
//
// Pill states:
//   * Passed (auto)    → green check + timestamp
//   * Passed (manual)  → green check + "manual" label + timestamp
//   * Next-expected    → highlighted blue + "Pass" CTA (primary action)
//   * Upcoming         → dim + small "Pass" link (long-tap to skip ahead)
//
// Spec — Stage 1 (2026-05-30): always-tappable per the user request.
// Tapping mark N also confirms every unpassed mark before N (backend
// backfill, see record_manual_mark_pass). For non-next-expected marks
// we show a confirmation alert because skipping ahead is a meaningful
// action ("are you sure you've passed marks 2 through 5?").

import { useCallback, useMemo } from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";

import type { MarkPass } from "../api/races";
import { useTheme } from "../theme/ThemeProvider";
import type { RaceMark } from "../types";

type Props = {
  marks: RaceMark[];
  passes: MarkPass[];
  /** Disables tap-to-pass — used after a successful pass while the
   *  refresh is in flight, or when the recorder isn't running. */
  disabled?: boolean;
  /** Returns the user's most recent (lat, lon) — attached to the
   *  tapped mark's pass record. May return null if no GPS fix yet. */
  getCurrentPosition?: () => { lat: number; lon: number } | null;
  /** Records the pass server-side (via useMarkPasses). */
  onMarkPass: (
    markIndex: number,
    opts?: { lat?: number; lon?: number },
  ) => Promise<unknown>;
  /** Truthy while a pass request is in flight. */
  pending?: boolean;
};

export function MarkPassControls({
  marks,
  passes,
  disabled,
  getCurrentPosition,
  onMarkPass,
  pending,
}: Props) {
  const { colors, font, size } = useTheme();

  // Sort + index for fast lookups.
  const passByIndex = useMemo(() => {
    const m = new Map<number, MarkPass>();
    for (const p of passes) m.set(p.mark_index, p);
    return m;
  }, [passes]);

  const nextExpected = useMemo(() => {
    // Lowest index without a pass — that's the auto-detector's next
    // target and the most likely "Pass" tap.
    for (let i = 0; i < marks.length; i += 1) {
      if (!passByIndex.has(i)) return i;
    }
    return marks.length; // course complete
  }, [marks.length, passByIndex]);

  const handlePass = useCallback(
    (markIndex: number, markName: string) => {
      const pos = getCurrentPosition?.() ?? null;
      const opts = pos ? { lat: pos.lat, lon: pos.lon } : undefined;
      const fire = () => {
        void onMarkPass(markIndex, opts).catch((e: unknown) => {
          Alert.alert(
            "Couldn't record pass",
            e instanceof Error ? e.message : String(e),
          );
        });
      };
      // Tapping the next-expected mark is the common case — fire
      // immediately. Tapping further-ahead marks implies "I missed
      // several roundings, mark all of them"; confirm because the
      // backfill is non-reversible.
      if (markIndex === nextExpected) {
        fire();
        return;
      }
      const skipped = Math.max(0, markIndex - nextExpected);
      Alert.alert(
        `Pass ${markName}?`,
        skipped > 0
          ? `This will also mark ${skipped} earlier mark${
              skipped === 1 ? "" : "s"
            } as passed.`
          : "Mark this rounding as confirmed.",
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Pass",
            style: "default",
            onPress: fire,
          },
        ],
      );
    },
    [nextExpected, onMarkPass, getCurrentPosition],
  );

  if (marks.length === 0) return null;

  return (
    <View style={styles.root} pointerEvents="box-none">
      {pending ? (
        <View style={styles.pendingBadge}>
          <ActivityIndicator size="small" color={colors.text.primary} />
        </View>
      ) : null}
      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        contentContainerStyle={styles.scrollContent}
      >
        {marks.map((mark, idx) => {
          const pass = passByIndex.get(idx);
          const isNext = idx === nextExpected;
          const isPassed = pass !== undefined;
          const isManual = pass?.source === "manual";

          // Visual treatment per state.
          let bg = colors.surface.floating;
          let labelColor = colors.text.primary;
          let captionColor = colors.text.muted;
          let borderColor = colors.border.hairline;
          if (isPassed) {
            bg = `${colors.accent.success}22`;
            borderColor = `${colors.accent.success}55`;
            captionColor = colors.accent.success;
          } else if (isNext) {
            bg = colors.accent.primary;
            labelColor = colors.text.onAccent;
            captionColor = colors.text.onAccent;
            borderColor = "transparent";
          } else {
            // upcoming — dim
            labelColor = colors.text.secondary;
          }

          return (
            <Pressable
              key={idx}
              onPress={() => handlePass(idx, mark.name)}
              disabled={disabled || isPassed}
              style={({ pressed }) => [
                styles.pill,
                {
                  backgroundColor: bg,
                  borderColor,
                  opacity: pressed && !isPassed ? 0.8 : 1,
                  shadowColor: colors.scrim.shadow,
                },
              ]}
              accessibilityRole="button"
              accessibilityLabel={
                isPassed
                  ? `${mark.name} passed`
                  : `Mark ${mark.name} passed`
              }
            >
              <View style={styles.pillTop}>
                <Text
                  style={{
                    color: captionColor,
                    fontFamily: font.bodyMedium,
                    fontSize: size.caption,
                    letterSpacing: 0.4,
                  }}
                >
                  {idx + 1}
                </Text>
                {isPassed ? (
                  <Ionicons
                    name="checkmark"
                    size={14}
                    color={colors.accent.success}
                  />
                ) : isNext ? (
                  <Ionicons
                    name="locate"
                    size={14}
                    color={colors.text.onAccent}
                  />
                ) : null}
              </View>
              <Text
                style={[
                  styles.pillName,
                  {
                    color: labelColor,
                    fontFamily: isNext ? font.bodySemibold : font.body,
                    fontSize: size.body,
                  },
                ]}
                numberOfLines={1}
              >
                {mark.name}
              </Text>
              <Text
                style={{
                  color: captionColor,
                  fontFamily: font.body,
                  fontSize: size.caption,
                }}
                numberOfLines={1}
              >
                {isPassed
                  ? `${formatTs(pass!.ts)}${isManual ? " · manual" : ""}`
                  : isNext
                    ? "Tap to pass"
                    : "Pass"}
              </Text>
            </Pressable>
          );
        })}
      </ScrollView>
    </View>
  );
}

function formatTs(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const styles = StyleSheet.create({
  root: {
    // The recording screen places this above the GuidanceCard. Its
    // own padding is handled by the parent's SafeAreaView container.
  },
  pendingBadge: {
    position: "absolute",
    right: 12,
    top: -10,
    zIndex: 1,
  },
  scrollContent: {
    paddingHorizontal: 4,
    gap: 8,
  },
  pill: {
    minWidth: 96,
    maxWidth: 140,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 14,
    borderWidth: StyleSheet.hairlineWidth,
    gap: 4,
    shadowOffset: { width: 0, height: 3 },
    shadowOpacity: 1,
    shadowRadius: 6,
    elevation: 2,
  },
  pillTop: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  pillName: {
    letterSpacing: -0.1,
  },
});
