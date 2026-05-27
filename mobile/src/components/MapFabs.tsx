// MapFabs.tsx — floating action buttons over the map.
//
// Google-Maps-style FAB cluster, top-right (under the compass). Each
// button is a circular surface with a single icon + soft shadow. Pure
// presentational — the parent owns the handlers + state (e.g., is the
// wind layer on?).
//
// Top to bottom:
//   - Layers toggle (turns wind barbs on/off — additive over time)
//   - Locate-me
//
// Spacing leaves room above the compass at the top of MapCanvas
// (top: 18 + ~36 compass + 16 gap = ~70px).

import { Pressable, StyleSheet, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useTheme } from "../theme/ThemeProvider";

type Props = {
  onLocateMe: () => void;
  onToggleWind: () => void;
  onToggleCompass: () => void;
  windOn: boolean;
  /** Current map heading in degrees (0 = north). Rotates the compass icon. */
  headingDeg: number;
};

export function MapFabs({
  onLocateMe,
  onToggleWind,
  onToggleCompass,
  windOn,
  headingDeg,
}: Props) {
  const { colors } = useTheme();
  const insets = useSafeAreaInsets();
  // 16px gap below the system status bar / notch on every device. With
  // the static `top: 16` we were sitting UNDER the status bar on most
  // Android phones because the MapView is full-bleed (no parent
  // SafeAreaView).
  const topOffset = insets.top + 16;
  return (
    <View style={[styles.cluster, { top: topOffset }]} pointerEvents="box-none">
      {/* Compass FAB — replaces the built-in Mapbox compass, which is
          display-only (no tap handler). Icon rotates with the map's
          heading so it visually represents which way is north; tapping
          animates back to heading=0. Matches the Google Maps compass
          behaviour the user expected. */}
      <Fab
        icon={
          <View style={{ transform: [{ rotate: `${-headingDeg}deg` }] }}>
            <Ionicons name="compass" size={22} color={colors.accent.recording} />
          </View>
        }
        onPress={onToggleCompass}
        accessibilityLabel="Toggle compass mode (north / follow heading)"
      />
      <Fab
        icon={
          <Ionicons
            name={windOn ? "layers" : "layers-outline"}
            size={22}
            color={windOn ? colors.accent.primary : colors.text.primary}
          />
        }
        onPress={onToggleWind}
        accessibilityLabel={windOn ? "Hide wind barbs" : "Show wind barbs"}
      />
      <Fab
        icon={<Ionicons name="navigate" size={22} color={colors.text.primary} />}
        onPress={onLocateMe}
        accessibilityLabel="Locate me"
      />
    </View>
  );
}

function Fab({
  icon,
  onPress,
  accessibilityLabel,
}: {
  icon: React.ReactNode;
  onPress: () => void;
  accessibilityLabel: string;
}) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={accessibilityLabel}
      style={({ pressed }) => [
        styles.fab,
        {
          backgroundColor: colors.surface.floating,
          shadowColor: colors.scrim.shadow,
          opacity: pressed ? 0.85 : 1,
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
    // `top` is set inline from useSafeAreaInsets so the cluster sits
    // below the notch / status bar on every device.
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
