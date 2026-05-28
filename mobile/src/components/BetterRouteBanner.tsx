// BetterRouteBanner.tsx — slot-in card for "faster route available".
//
// Designed to live INSIDE the bottom sheet (above the race header) — not
// as a floating absolute overlay like the web. The mobile screen is too
// small to spare 60px of top chrome for a banner; tucking it into the
// sheet means the user sees it while reading race details and the map
// behind stays uncluttered.
//
// AUTO-ACCEPT MODE (inverted Google Maps "use new route" prompt):
// When `autoAcceptSeconds > 0` and the parent's auto-route setting is on,
// the banner counts down to auto-accept. The user taps **Decline** to
// cancel and keep the current route. This matches the user's stated
// preference — racing UX should default to the smarter route unless the
// skipper actively says no.
//
// When `autoAcceptSeconds === 0` (or the toggle is off), the banner shows
// "Use / Decline" buttons in the old manual mode.

import { useEffect, useRef, useState } from "react";
import { Animated, Easing, Pressable, StyleSheet, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";

import { useTheme } from "../theme/ThemeProvider";
import type { AlternativePayload } from "../hooks/useRouteNotifications";

type Props = {
  alternative: AlternativePayload | null;
  onAccept: () => void;
  onDismiss: () => void;
  /**
   * Seconds before the banner auto-accepts. 0 = manual mode (old
   * behaviour: explicit "Use" tap). Default 10.
   */
  autoAcceptSeconds?: number;
};

export function BetterRouteBanner({
  alternative,
  onAccept,
  onDismiss,
  autoAcceptSeconds = 10,
}: Props) {
  const { colors, font, size, tabularVariant } = useTheme();
  const auto = autoAcceptSeconds > 0;

  // Countdown timer. Resets whenever a new alternative arrives.
  const [secondsLeft, setSecondsLeft] = useState(autoAcceptSeconds);
  // Animated progress 0→1 used to fill the countdown ring.
  const progress = useRef(new Animated.Value(0)).current;
  // Avoid re-firing onAccept after we've already fired once for this alt.
  const firedRef = useRef(false);

  // Stable key per alternative so we can detect "new arrival" and reset.
  const altKey = alternative
    ? `${alternative.race_id}:${alternative.computed_at}`
    : null;

  useEffect(() => {
    firedRef.current = false;
    setSecondsLeft(autoAcceptSeconds);
    progress.setValue(0);
    if (!alternative || !auto) return;

    Animated.timing(progress, {
      toValue: 1,
      duration: autoAcceptSeconds * 1000,
      easing: Easing.linear,
      useNativeDriver: false,
    }).start();

    const tickId = setInterval(() => {
      setSecondsLeft((s) => Math.max(0, s - 1));
    }, 1000);
    const fireId = setTimeout(() => {
      if (!firedRef.current) {
        firedRef.current = true;
        onAccept();
      }
    }, autoAcceptSeconds * 1000);

    return () => {
      clearInterval(tickId);
      clearTimeout(fireId);
      progress.stopAnimation();
    };
    // `auto` and `autoAcceptSeconds` are read at start-of-effect; we
    // reset cleanly when the alternative changes (altKey), and we don't
    // want each re-render that captures a new onAccept to reset the
    // timer. Keep deps narrow.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [altKey, auto, autoAcceptSeconds]);

  if (!alternative) return null;

  const mins = Math.round(alternative.improvement_minutes);
  const pct = alternative.improvement_pct.toFixed(1);

  const handleDecline = () => {
    firedRef.current = true; // prevent the auto-accept timer from firing
    progress.stopAnimation();
    onDismiss();
  };

  const handleUseNow = () => {
    if (firedRef.current) return;
    firedRef.current = true;
    progress.stopAnimation();
    onAccept();
  };

  // Width-based progress fill behind the banner content. We use a width
  // animation rather than an SVG ring because it adds zero deps and reads
  // clearly at this size — the bar visually empties as time runs out.
  const fillWidth = progress.interpolate({
    inputRange: [0, 1],
    outputRange: ["100%", "0%"],
  });

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
      {/* Countdown progress bar — only visible in auto-accept mode. */}
      {auto ? (
        <Animated.View
          pointerEvents="none"
          style={[
            styles.progressFill,
            {
              backgroundColor: `${colors.accent.route}26`,
              width: fillWidth,
            },
          ]}
        />
      ) : null}

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
          {auto ? "Switching to faster route" : "Faster route available"}
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
          {auto && secondsLeft > 0 ? (
            <Text style={{ color: colors.text.muted }}>{`  ·  ${secondsLeft}s`}</Text>
          ) : null}
        </Text>
      </View>
      <View style={styles.actions}>
        {auto ? (
          <Pressable
            onPress={handleDecline}
            accessibilityLabel="Decline new route, keep current"
            style={({ pressed }) => [
              styles.declineBtn,
              {
                borderColor: colors.border.divider,
                backgroundColor: pressed
                  ? colors.surface.elevated
                  : "transparent",
              },
            ]}
          >
            <Text
              style={{
                color: colors.text.primary,
                fontFamily: font.bodySemibold,
                fontSize: size.small,
              }}
            >
              Decline
            </Text>
          </Pressable>
        ) : (
          <>
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
              onPress={handleUseNow}
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
          </>
        )}
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
    overflow: "hidden",
  },
  progressFill: {
    position: "absolute",
    top: 0,
    bottom: 0,
    left: 0,
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
  declineBtn: {
    paddingHorizontal: 12,
    height: 32,
    borderRadius: 16,
    borderWidth: StyleSheet.hairlineWidth,
    alignItems: "center",
    justifyContent: "center",
  },
});
