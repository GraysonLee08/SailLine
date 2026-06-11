// TacticianCard.tsx — the on-screen surface for AI tactician calls.
//
// Renders the most recent call as a dismissible card in the recording
// screen's bottom stack (above the guidance card). Design constraints
// from the spec + dev plan §3.3 "Sunlight" rules:
//   * large type — readable at arm's length on a bouncing deck,
//   * glove-sized dismiss target (44pt min),
//   * haptic pulse on a NEW call (no audio — quiet cockpit),
//   * auto-expires: a maneuver call disappears once its moment has
//     passed; a coaching call lingers a bit longer.
//
// Vibration comes from react-native core (no new native dependency);
// expo-haptics would be marginally nicer but isn't installed and a
// pattern buzz is indistinguishable through a phone mount anyway.

import { useEffect, useRef, useState } from "react";
import { Pressable, StyleSheet, Text, Vibration, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";

import type { TacticsPayload } from "../hooks/useRouteNotifications";
import { useTheme } from "../theme/ThemeProvider";

// How long a call stays on screen without dismissal.
const MANEUVER_TTL_MS = 4 * 60_000; // covers the announce window
const COACHING_TTL_MS = 2 * 60_000;

// Two short pulses — distinct from the OS notification buzz.
const VIBRATE_PATTERN = [0, 200, 120, 200];

type Props = {
  call: TacticsPayload | null;
  onDismiss: () => void;
};

function labelFor(callType: string): string {
  switch (callType) {
    case "planned_maneuver":
      return "MANEUVER AHEAD";
    case "layline":
      return "LAYLINE";
    case "forecast_shift":
      return "WIND AHEAD";
    case "over_heel":
      return "HEEL";
    case "pinching":
      return "ANGLE";
    case "off_pace":
      return "PACE";
    case "plan_divergence":
      return "OFF PLAN";
    default:
      return "TACTICIAN";
  }
}

export function TacticianCard({ call, onDismiss }: Props) {
  const { colors, font, size } = useTheme();
  const [visible, setVisible] = useState(false);
  const lastSeenRef = useRef<string | null>(null);

  // New-call lifecycle: show + buzz on a payload we haven't seen,
  // auto-expire on the class-appropriate TTL.
  useEffect(() => {
    if (!call) {
      setVisible(false);
      return undefined;
    }
    if (call.created_at === lastSeenRef.current) return undefined;
    lastSeenRef.current = call.created_at;
    setVisible(true);
    try {
      Vibration.vibrate(VIBRATE_PATTERN);
    } catch {
      /* simulator / no vibrator */
    }
    const ttl =
      call.call_class === "maneuver" ? MANEUVER_TTL_MS : COACHING_TTL_MS;
    const t = setTimeout(() => setVisible(false), ttl);
    return () => clearTimeout(t);
  }, [call]);

  if (!call || !visible) return null;

  return (
    <View
      style={[
        styles.card,
        {
          backgroundColor: colors.surface.floating,
          borderColor: colors.accent.primary,
          shadowColor: colors.scrim.shadow,
        },
      ]}
      accessibilityRole="alert"
    >
      <View style={styles.header}>
        <View style={styles.headerLeft}>
          <Ionicons
            name="navigate"
            size={14}
            color={colors.accent.primary}
          />
          <Text
            style={{
              color: colors.accent.primary,
              fontFamily: font.bodyBold,
              fontSize: size.caption,
              letterSpacing: 0.8,
            }}
          >
            {labelFor(call.call_type)}
          </Text>
        </View>
        <Pressable
          onPress={() => {
            setVisible(false);
            onDismiss();
          }}
          accessibilityLabel="Dismiss tactician call"
          hitSlop={10}
          style={({ pressed }) => [
            styles.dismiss,
            { opacity: pressed ? 0.6 : 1 },
          ]}
        >
          <Ionicons name="close" size={22} color={colors.text.primary} />
        </Pressable>
      </View>
      <Text
        style={{
          color: colors.text.primary,
          fontFamily: font.bodySemibold,
          fontSize: size.bodyLg,
          lineHeight: size.bodyLg * 1.35,
        }}
      >
        {call.message}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    borderRadius: 16,
    borderWidth: 1.5,
    paddingHorizontal: 16,
    paddingTop: 10,
    paddingBottom: 14,
    gap: 6,
    shadowOffset: { width: 0, height: 3 },
    shadowOpacity: 1,
    shadowRadius: 8,
    elevation: 4,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  headerLeft: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  dismiss: {
    width: 44,
    height: 44,
    alignItems: "center",
    justifyContent: "center",
    marginRight: -10,
    marginTop: -8,
  },
});
