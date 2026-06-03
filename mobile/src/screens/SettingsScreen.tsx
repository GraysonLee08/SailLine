// SettingsScreen.tsx — global app settings.
//
// 2026-06-03 — minimum-viable surface for the menu launch. Holds the
// auto-pass toggle (B2) and a link into the recorder-debug screen.
// More toggles will arrive here as we split webapp parity items —
// theme, units, default boat, debug feature flags. For now this is
// thin on purpose: ship the toggle the user asked for, leave the rest
// for follow-ups.
//
// The "Settings vs Profile" split (per the 2026-06-02 plan):
//   * Settings = how the APP behaves (this screen).
//   * Profile  = who I AM (ProfileScreen).
// The webapp conflates these today; mobile is the first surface that
// splits them. Backend parity (single user_profiles row) is unchanged.

import { ScrollView, StyleSheet, Switch, Text, View, Pressable } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { router } from "expo-router";

import { useAutoPassSetting } from "../hooks/useAutoPassSetting";
import { useTheme } from "../theme/ThemeProvider";

export function SettingsScreen() {
  const { colors, font, size } = useTheme();
  const insets = useSafeAreaInsets();
  const autoPass = useAutoPassSetting();

  return (
    <View style={[styles.root, { backgroundColor: colors.surface.page }]}>
      <View
        style={[styles.header, { paddingTop: insets.top + 8, borderColor: colors.border.hairline }]}
      >
        <Pressable
          onPress={() => router.back()}
          accessibilityRole="button"
          accessibilityLabel="Back"
          hitSlop={12}
        >
          <Ionicons name="chevron-back" size={26} color={colors.text.primary} />
        </Pressable>
        <Text
          style={{
            color: colors.text.primary,
            fontFamily: font.displaySemibold,
            fontSize: size.title,
          }}
        >
          Settings
        </Text>
        <View style={{ width: 26 }} />
      </View>

      <ScrollView
        contentContainerStyle={[
          styles.body,
          { paddingBottom: insets.bottom + 24 },
        ]}
      >
        <Section title="Race recording">
          <Row
            label="Auto-detect mark passes"
            description="When on, the app watches your track and alerts you if it looks like you rounded a mark the detector missed. Turn off to only mark passes manually."
            right={
              <Switch
                value={autoPass.enabled}
                onValueChange={autoPass.setEnabled}
                trackColor={{
                  true: colors.accent.primary,
                  false: colors.border.hairline,
                }}
                thumbColor={colors.surface.elevated}
              />
            }
          />
        </Section>

        <Section title="Diagnostics">
          <NavRow
            label="Recorder debug"
            description="Live capture stats, upload log, queue depth."
            onPress={() => router.push("/recorder-debug")}
          />
        </Section>

        <Text
          style={{
            color: colors.text.muted,
            fontFamily: font.body,
            fontSize: size.small,
            marginTop: 16,
            textAlign: "center",
          }}
        >
          More settings — units, theme, default boat — coming as the
          webapp parity work continues.
        </Text>
      </ScrollView>
    </View>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  const { colors, font, size } = useTheme();
  return (
    <View style={{ marginTop: 18 }}>
      <Text
        style={{
          color: colors.text.muted,
          fontFamily: font.body,
          fontSize: size.caption,
          letterSpacing: 0.8,
          textTransform: "uppercase",
          marginBottom: 6,
        }}
      >
        {title}
      </Text>
      <View
        style={[
          styles.card,
          {
            backgroundColor: colors.surface.elevated,
            borderColor: colors.border.hairline,
          },
        ]}
      >
        {children}
      </View>
    </View>
  );
}

function Row({
  label,
  description,
  right,
}: {
  label: string;
  description?: string;
  right?: React.ReactNode;
}) {
  const { colors, font, size } = useTheme();
  return (
    <View style={styles.row}>
      <View style={{ flex: 1, paddingRight: 12 }}>
        <Text
          style={{
            color: colors.text.primary,
            fontFamily: font.bodyMedium,
            fontSize: size.body,
          }}
        >
          {label}
        </Text>
        {description ? (
          <Text
            style={{
              color: colors.text.muted,
              fontFamily: font.body,
              fontSize: size.small,
              marginTop: 4,
            }}
          >
            {description}
          </Text>
        ) : null}
      </View>
      {right}
    </View>
  );
}

function NavRow({
  label,
  description,
  onPress,
}: {
  label: string;
  description?: string;
  onPress: () => void;
}) {
  const { colors } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={label}
    >
      <Row
        label={label}
        description={description}
        right={
          <Ionicons
            name="chevron-forward"
            size={18}
            color={colors.text.muted}
          />
        }
      />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 14,
    paddingBottom: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  body: {
    paddingHorizontal: 18,
    paddingTop: 8,
  },
  card: {
    borderRadius: 14,
    borderWidth: StyleSheet.hairlineWidth,
    paddingHorizontal: 14,
    paddingVertical: 4,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    paddingVertical: 14,
  },
});
