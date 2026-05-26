// BetterRouteBanner.tsx — slot-in card for "faster route available".
//
// Designed to live INSIDE the bottom sheet (above the race header) — not
// as a floating absolute overlay like the web. The mobile screen is too
// small to spare 60px of top chrome for a banner; tucking it into the
// sheet means the user sees it while reading race details and the map
// behind stays uncluttered.
//
// Numbers shown with the tabular font for a steady racing-instrument
// feel ("-3 min · 4.8% faster"). No motion library on this one — RN
// reanimated would be overkill for a single mount-time accent.

import { Pressable, StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";

import { useTheme } from "../theme/ThemeProvider";
import type { AlternativePayload } from "../hooks/useRouteNotifications";

type Props = {
  alternative: AlternativePayload | null;
  onAccept: () => void;
  onDismiss: () => void;
};

export function BetterRouteBanner({ alternative, onAccept, onDismiss }: Props) {
  const { colors, font, size, tabularVariant } = useTheme();
  if (!alternative) return null;

  const mins = Math.round(alternative.improvement_minutes);
  const pct = alternative.improvement_pct.toFixed(1);

  return (
    <View
      accessibilityRole="alert"
      style={[
        styles.banner,
        {
          backgroundColor: `${colors.accent.route}1c`,
          borderColor: `${colors.accent.route}55`,
        },
      ]}
    >
      <View
        style={[styles.iconWrap, { backgroundColor: `${colors.accent.route}33` }]}
      >
        <Ionicons name="flash" size={18} color={colors.accent.route} />
      </View>
      <View style={{ flex: 1 }}>
        <Text
          style={{
            color: colors.text.primary,
            fontFamily: font.bodySemibold,
            fontSize: size.body,
          }}
        >
          Faster route available
        </Text>
        <Text
          style={[
            {
              color: colors.text.secondary,
              fontFamily: font.tabular,
              fontSize: size.small,
              marginTop: 2,
            },
            tabularVariant,
          ]}
        >
          Save{" "}
          <Text style={{ fontFamily: font.tabularBold, color: colors.text.primary }}>
            {mins} min
          </Text>{" "}
          · {pct}% faster
        </Text>
      </View>
      <View style={styles.actions}>
        <Pressable
          onPress={onDismiss}
          accessibilityLabel="Dismiss"
          hitSlop={8}
          style={({ pressed }) => [
            styles.dismiss,
            { opacity: pressed ? 0.6 : 1 },
          ]}
        >
          <Ionicons name="close" size={18} color={colors.text.muted} />
        </Pressable>
        <Pressable
          onPress={onAccept}
          accessibilityLabel="Use the faster route"
          style={({ pressed }) => [
            styles.accept,
            {
              backgroundColor: pressed
                ? colors.accent.primaryPressed
                : colors.accent.route,
            },
          ]}
        >
          <Text
            style={{
              color: colors.text.onAccent,
              fontFamily: font.bodySemibold,
              fontSize: size.small,
            }}
          >
            Use
          </Text>
        </Pressable>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 12,
    borderWidth: StyleSheet.hairlineWidth,
  },
  iconWrap: {
    width: 32,
    height: 32,
    borderRadius: 16,
    alignItems: "center",
    justifyContent: "center",
  },
  actions: { flexDirection: "row", alignItems: "center", gap: 6 },
  dismiss: {
    width: 32,
    height: 32,
    borderRadius: 16,
    alignItems: "center",
    justifyContent: "center",
  },
  accept: {
    paddingHorizontal: 14,
    height: 32,
    borderRadius: 16,
    alignItems: "center",
    justifyContent: "center",
  },
});
