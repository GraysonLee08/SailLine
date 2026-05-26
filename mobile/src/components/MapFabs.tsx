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

import { useTheme } from "../theme/ThemeProvider";

type Props = {
  onLocateMe: () => void;
  onToggleWind: () => void;
  windOn: boolean;
};

export function MapFabs({ onLocateMe, onToggleWind, windOn }: Props) {
  const { colors } = useTheme();
  return (
    <View style={styles.cluster} pointerEvents="box-none">
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
    top: 76, // below the compass (top: 18 + ~36 + 22 gap)
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
