// PermissionWelcomeCard.tsx — one-time first-launch permission ask.
//
// Shown once per install (gated by AsyncStorage flag, see below). Lays
// out the three OS-level permissions the recorder needs alongside an
// honest explanation of what each unlocks, then runs all three prompts
// in sequence when the user taps "Get started". Each row's status flips
// to a checkmark / warning badge after the OS prompt resolves so the
// user can see what they actually granted before dismissing the card.
//
// Why a card, not lazy prompts:
// Previously notifications was the only thing prompted up-front (at
// sign-in). Location was prompted lazily by BackgroundGeolocation.ready()
// when the user first tapped Start, and activity recognition was never
// requested at all. The user reported (2026-06-04) that they only ever
// saw the notifications dialog and assumed the rest were configured.
// Putting all three behind one explained surface fixes that.
//
// Honest copy on degradation modes:
//   * Foreground location alone → blue dot on map + centre FAB work,
//     but recording pauses when the screen locks.
//   * Background ("Always") → recording stays live with the phone in
//     a pocket. This is the load-bearing one for race day.
//   * Activity → battery savings only; recorder still works without.
//   * Notifications → race-start reminders, missed-mark prompts, route
//     updates. Silently no-op without.
//
// Storage flag: @sailline.welcome.seen.v1. Bumping the suffix forces
// the card to re-show after a release that adds a new permission ask.

import { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Modal,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";
import AsyncStorage from "@react-native-async-storage/async-storage";

import {
  requestRecorderPermissions,
  type PermissionResult,
  type RecorderPermissionStatus,
} from "../recorder/permissions";
import { useTheme } from "../theme/ThemeProvider";

const STORAGE_KEY = "@sailline.welcome.seen.v1";

type RowKey = "notifications" | "locationAlways" | "activity";

type RowSpec = {
  key: RowKey;
  icon: keyof typeof Ionicons.glyphMap;
  title: string;
  /** What the user gets if they GRANT. */
  benefit: string;
  /** What the user loses if they DECLINE — shown after a denial. */
  degradation: string;
};

const ROWS: RowSpec[] = [
  {
    key: "notifications",
    icon: "notifications-outline",
    title: "Notifications",
    benefit:
      "Race-start reminders six minutes before the gun, missed-mark prompts mid-race, and route-update alerts.",
    degradation:
      "Without notifications these reminders silently no-op. Recording still works; you'll need to start it manually.",
  },
  {
    key: "locationAlways",
    icon: "navigate-outline",
    title: "Location (Always)",
    benefit:
      "Shows your boat on the map and keeps recording when your screen is off or the phone is in your pocket.",
    degradation:
      "Without 'Always' the blue dot works but recording stops every time the screen locks. Switch to Always in Settings before race day.",
  },
  {
    key: "activity",
    icon: "fitness-outline",
    title: "Motion & Fitness",
    benefit:
      "Lets SailLine throttle GPS polling when the boat is moored, saving battery on long days at the dock.",
    degradation:
      "Optional. Without it the recorder still captures fixes — battery drain just runs a bit higher.",
  },
];

type Props = {
  /** Whether the auth gate considers the user signed in. The card only
   *  shows for signed-in users; sign-in screen has its own surface. */
  visible: boolean;
};

/**
 * Top-level gate: read the seen flag once, render the card if unseen.
 * The flag is written the moment the user dismisses — whether by
 * completing the prompts or by tapping "Skip for now".
 */
export function PermissionWelcomeCard({ visible }: Props) {
  const [seen, setSeen] = useState<boolean | null>(null); // null = loading

  // Hydrate once.
  useEffect(() => {
    void (async () => {
      try {
        const v = await AsyncStorage.getItem(STORAGE_KEY);
        setSeen(v === "1");
      } catch {
        // Storage failures shouldn't trap the user behind the card forever.
        // Treat as seen so the app proceeds; Settings still works for them
        // to grant permissions later.
        setSeen(true);
      }
    })();
  }, []);

  const handleDone = useCallback(async () => {
    try {
      await AsyncStorage.setItem(STORAGE_KEY, "1");
    } finally {
      setSeen(true);
    }
  }, []);

  if (!visible || seen === null || seen === true) return null;

  return <WelcomeModal onDone={handleDone} />;
}

function WelcomeModal({ onDone }: { onDone: () => void }) {
  const { colors, font, size } = useTheme();
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState<RecorderPermissionStatus | null>(null);

  const onStart = useCallback(async () => {
    setRunning(true);
    try {
      const result = await requestRecorderPermissions();
      setStatus(result);
    } finally {
      setRunning(false);
    }
  }, []);

  // Once the user has seen results, the primary CTA flips to "Done".
  const allAnswered = status !== null;

  return (
    <Modal
      visible
      animationType="fade"
      transparent
      statusBarTranslucent
      onRequestClose={onDone}
    >
      <View style={[styles.scrim, { backgroundColor: colors.scrim.overlay }]}>
        <View
          style={[
            styles.card,
            {
              backgroundColor: colors.surface.elevated,
              borderColor: colors.border.hairline,
            },
          ]}
        >
          <Text
            style={{
              color: colors.text.primary,
              fontFamily: font.bodyBold,
              fontSize: size.title,
              marginBottom: 4,
            }}
          >
            Set up SailLine
          </Text>
          <Text
            style={{
              color: colors.text.muted,
              fontFamily: font.body,
              fontSize: size.body,
              marginBottom: 16,
            }}
          >
            SailLine needs a few permissions before race day. You can
            change any of these later in Settings.
          </Text>

          {ROWS.map((row) => (
            <PermissionRow
              key={row.key}
              spec={row}
              result={status?.[row.key] ?? null}
            />
          ))}

          <Pressable
            onPress={allAnswered ? onDone : onStart}
            disabled={running}
            accessibilityRole="button"
            accessibilityLabel={allAnswered ? "Done" : "Get started"}
            style={({ pressed }) => [
              styles.cta,
              {
                backgroundColor: pressed
                  ? colors.accent.primaryPressed
                  : colors.accent.primary,
                opacity: running ? 0.6 : 1,
              },
            ]}
          >
            {running ? (
              <ActivityIndicator color={colors.text.onAccent} />
            ) : (
              <Text
                style={{
                  color: colors.text.onAccent,
                  fontFamily: font.bodySemibold,
                  fontSize: size.bodyLg,
                }}
              >
                {allAnswered ? "Done" : "Get started"}
              </Text>
            )}
          </Pressable>

          {!allAnswered ? (
            <Pressable onPress={onDone} disabled={running} style={styles.skip}>
              <Text
                style={{
                  color: colors.text.muted,
                  fontFamily: font.body,
                  fontSize: size.small,
                  textAlign: "center",
                }}
              >
                Skip for now
              </Text>
            </Pressable>
          ) : null}
        </View>
      </View>
    </Modal>
  );
}

function PermissionRow({
  spec,
  result,
}: {
  spec: RowSpec;
  result: PermissionResult | null;
}) {
  const { colors, font, size } = useTheme();
  // Body text flips between benefit (pre-prompt) and degradation
  // (post-prompt, denied) so the user sees the consequence of a no.
  const body =
    result === "denied" ? spec.degradation : spec.benefit;
  return (
    <View
      style={[
        styles.row,
        {
          borderColor: colors.border.hairline,
        },
      ]}
    >
      <View style={styles.rowIcon}>
        <Ionicons name={spec.icon} size={22} color={colors.text.primary} />
      </View>
      <View style={styles.rowText}>
        <View style={styles.rowTitleLine}>
          <Text
            style={{
              color: colors.text.primary,
              fontFamily: font.bodyMedium,
              fontSize: size.body,
            }}
          >
            {spec.title}
          </Text>
          <StatusBadge result={result} />
        </View>
        <Text
          style={{
            color: colors.text.muted,
            fontFamily: font.body,
            fontSize: size.small,
            marginTop: 2,
          }}
        >
          {body}
        </Text>
      </View>
    </View>
  );
}

function StatusBadge({ result }: { result: PermissionResult | null }) {
  const { colors } = useTheme();
  if (result === null) return null;
  const tone =
    result === "granted"
      ? colors.accent.success
      : result === "unavailable"
        ? colors.text.muted
        : colors.accent.warning;
  const icon: keyof typeof Ionicons.glyphMap =
    result === "granted" ? "checkmark-circle" : "alert-circle";
  return <Ionicons name={icon} size={18} color={tone} />;
}

const styles = StyleSheet.create({
  scrim: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 20,
  },
  card: {
    width: "100%",
    maxWidth: 420,
    borderRadius: 18,
    padding: 20,
    borderWidth: StyleSheet.hairlineWidth,
  },
  row: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 12,
    paddingVertical: 12,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  rowIcon: {
    width: 28,
    alignItems: "center",
    paddingTop: 2,
  },
  rowText: {
    flex: 1,
  },
  rowTitleLine: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 8,
  },
  cta: {
    height: 50,
    borderRadius: 25,
    alignItems: "center",
    justifyContent: "center",
    marginTop: 16,
  },
  skip: {
    marginTop: 10,
    paddingVertical: 6,
  },
});
