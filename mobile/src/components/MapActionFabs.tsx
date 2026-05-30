// MapActionFabs.tsx — bottom-right action FAB cluster (Google Maps style).
//
// Sibling of MapFabs.tsx, which lives top-right and holds map-state
// controls (compass / wind layer / locate-me). This cluster holds
// race-action FABs and is only rendered when a race is selected:
//
//   - Start FAB (top)        → Start recording.
//                              Hidden once `recording === true`
//                              (mirrors the in-sheet primary CTA which
//                              also disappears).
//   - Minimize FAB (bottom)  → Collapses the race detail sheet to the
//                              peek snap point. Only visible when the
//                              sheet is expanded — matches Google Maps
//                              where the X only appears when the place
//                              sheet is open.
//
// History — 2026-05-29 the cluster also had a Directions (Compute /
// Recompute) FAB at the top, mirroring Google Maps' Directions button.
// Removed 2026-05-29 (evening) because it duplicated the Recompute
// button already inside the race detail sheet header. The Recompute
// button in the sheet is the single entry point for route compute now.
//
// Phase 1 spec: 2026-05-29_mobile-ui-google-maps-mapping.md §4.
//
// The cluster intentionally duplicates the Start CTA already inside the
// RaceDetailSheet. This is the Google Maps pattern: the bottom sheet
// holds detail; the map holds the always-reachable action surface. The
// handlers are the same — we share a callback rather than duplicating
// state.

import { Pressable, StyleSheet, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useTheme } from "../theme/ThemeProvider";

type Props = {
  onStart: () => void;
  onMinimize: () => void;
  /** Hides the Start FAB once recording is live. */
  recording: boolean;
  /** Whether the race detail sheet is currently expanded (not at peek).
      The Minimize FAB only renders when this is true. */
  sheetExpanded: boolean;
};

export function MapActionFabs({
  onStart,
  onMinimize,
  recording,
  sheetExpanded,
}: Props) {
  const { colors } = useTheme();
  const insets = useSafeAreaInsets();
  // The race detail sheet's peek snap is 32% of screen height. We sit
  // above that with a small gap so the cluster never overlaps the sheet
  // handle. 32% of a typical phone (~850px) ≈ 272px; add a 24px gap.
  // Using bottom inset keeps the cluster off the gesture bar on iOS.
  const bottomOffset = insets.bottom + 296;

  return (
    <View
      style={[styles.cluster, { bottom: bottomOffset }]}
      pointerEvents="box-none"
    >
      {!recording ? (
        <Fab
          icon={
            <Ionicons
              name="radio-button-on"
              size={22}
              color={colors.text.onAccent}
            />
          }
          onPress={onStart}
          accessibilityLabel="Start recording"
          tone="accent"
        />
      ) : null}
      {sheetExpanded ? (
        <Fab
          icon={
            <Ionicons name="close" size={22} color={colors.text.primary} />
          }
          onPress={onMinimize}
          accessibilityLabel="Minimize race details"
        />
      ) : null}
    </View>
  );
}

function Fab({
  icon,
  onPress,
  accessibilityLabel,
  disabled,
  tone,
}: {
  icon: React.ReactNode;
  onPress: () => void;
  accessibilityLabel: string;
  disabled?: boolean;
  /** "accent" gives the FAB a coloured background (used for Start). */
  tone?: "accent";
}) {
  const { colors } = useTheme();
  const background =
    tone === "accent" ? colors.accent.primary : colors.surface.floating;
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      style={({ pressed }) => [
        styles.fab,
        {
          backgroundColor: background,
          shadowColor: colors.scrim.shadow,
          opacity: disabled ? 0.5 : pressed ? 0.85 : 1,
          borderColor: colors.border.hairline,
        },
      ]}
    >
      {icon}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  cluster: {
    position: "absolute",
    // `bottom` is set inline from useSafeAreaInsets so the cluster sits
    // above the peek-snap sheet and the gesture bar on every device.
    right: 16,
    gap: 12,
  },
  fab: {
    width: 48,
    height: 48,
    borderRadius: 24,
    alignItems: "center",
    justifyContent: "center",
    borderWidth: StyleSheet.hairlineWidth,
    // Native shadow (iOS) + elevation (Android).
    shadowOffset: { width: 0, height: 4 },
    shadowOpacity: 1,
    shadowRadius: 10,
    elevation: 4,
  },
});
