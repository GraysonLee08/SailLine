// MapActionFabs.tsx — bottom-right action FAB cluster (Google Maps style).
//
// Sibling of MapFabs.tsx, which lives top-right and holds map-state
// controls (compass / wind layer / locate-me). This cluster holds
// race-action FABs and is only rendered when a race is selected:
//
//   - Minimize FAB → Collapses the race detail sheet to the peek snap
//                    point. Only visible when the sheet is expanded —
//                    matches Google Maps where the X only appears when
//                    the place sheet is open.
//
// History
//   - 2026-05-29 the cluster also had a Directions (Compute / Recompute)
//     FAB. Removed because it duplicated the Recompute button already
//     inside the race detail sheet header.
//   - 2026-06-03 the cluster also had a Start FAB. Removed for the same
//     reason: the RaceDetailSheet already exposes a prominent Start CTA
//     and the user reported seeing "two recording FABs" when looking at
//     the home screen. The sheet's Start button is the single entry
//     point for going into recording mode; this cluster is now purely
//     about sheet-management UI (Minimize), not action UI.
//
// Phase 1 spec: 2026-05-29_mobile-ui-google-maps-mapping.md §4.

import { Pressable, StyleSheet, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useTheme } from "../theme/ThemeProvider";

type Props = {
  onMinimize: () => void;
  /** Whether the race detail sheet is currently expanded (not at peek).
      The Minimize FAB only renders when this is true. */
  sheetExpanded: boolean;
};

export function MapActionFabs({ onMinimize, sheetExpanded }: Props) {
  const { colors } = useTheme();
  const insets = useSafeAreaInsets();
  // The race detail sheet's peek snap is 32% of screen height. We sit
  // above that with a small gap so the cluster never overlaps the sheet
  // handle. 32% of a typical phone (~850px) ≈ 272px; add a 24px gap.
  // Using bottom inset keeps the cluster off the gesture bar on iOS.
  const bottomOffset = insets.bottom + 296;

  // Nothing to render when the sheet is at peek — the only FAB this
  // cluster owns today is Minimize, which is sheet-expanded-only. Keep
  // the View around so the parent's pointerEvents stacking stays
  // consistent across both states.
  return (
    <View
      style={[styles.cluster, { bottom: bottomOffset }]}
      pointerEvents="box-none"
    >
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
}: {
  icon: React.ReactNode;
  onPress: () => void;
  accessibilityLabel: string;
  disabled?: boolean;
}) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      style={({ pressed }) => [
        styles.fab,
        {
          backgroundColor: colors.surface.floating,
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
