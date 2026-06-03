// ProfileScreen.tsx — "who I am" surface.
//
// 2026-06-03 — minimum-viable parity with the webapp's Settings page.
// Shows the signed-in identity (name + email + profile picture if
// Google Sign-In provided one) plus the sign-out action. Per the
// 2026-06-02 plan, this is the SPLIT: app prefs live in Settings;
// user identity lives here.
//
// Stub — fuller parity (sail club, home port, default boat, units) will
// arrive as a follow-up commit. The backend already stores most of
// these on user_profiles; we just need editable fields on this screen.
// Flagged in the session summary.

import { StyleSheet, Text, View, Pressable, Image } from "react-native";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import { Ionicons } from "@expo/vector-icons";
import { router } from "expo-router";

import { useAuth } from "../auth/AuthContext";
import { useTheme } from "../theme/ThemeProvider";

export function ProfileScreen() {
  const { colors, font, size } = useTheme();
  const insets = useSafeAreaInsets();
  const { user, signOut } = useAuth();

  return (
    <View style={[styles.root, { backgroundColor: colors.surface.page }]}>
      <View
        style={[
          styles.header,
          { paddingTop: insets.top + 8, borderColor: colors.border.hairline },
        ]}
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
          Profile
        </Text>
        <View style={{ width: 26 }} />
      </View>

      <View style={[styles.body, { paddingBottom: insets.bottom + 24 }]}>
        <View style={styles.identityCard}>
          {user?.photoURL ? (
            <Image
              source={{ uri: user.photoURL }}
              style={[
                styles.avatar,
                { backgroundColor: colors.surface.elevated },
              ]}
            />
          ) : (
            <View
              style={[
                styles.avatar,
                {
                  backgroundColor: colors.surface.elevated,
                  alignItems: "center",
                  justifyContent: "center",
                  borderColor: colors.border.hairline,
                  borderWidth: StyleSheet.hairlineWidth,
                },
              ]}
            >
              <Ionicons
                name="person"
                size={42}
                color={colors.text.muted}
              />
            </View>
          )}
          <Text
            style={{
              color: colors.text.primary,
              fontFamily: font.displaySemibold,
              fontSize: size.title,
              marginTop: 16,
              textAlign: "center",
            }}
          >
            {user?.displayName ?? "Sailor"}
          </Text>
          {user?.email ? (
            <Text
              style={{
                color: colors.text.muted,
                fontFamily: font.body,
                fontSize: size.body,
                marginTop: 4,
                textAlign: "center",
              }}
            >
              {user.email}
            </Text>
          ) : null}
        </View>

        <View style={{ flex: 1 }} />

        <Pressable
          onPress={signOut}
          accessibilityRole="button"
          accessibilityLabel="Sign out"
          style={({ pressed }) => [
            styles.signOut,
            {
              backgroundColor: colors.surface.elevated,
              borderColor: colors.border.hairline,
              opacity: pressed ? 0.85 : 1,
            },
          ]}
        >
          <Ionicons
            name="log-out-outline"
            size={20}
            color={colors.accent.recording}
          />
          <Text
            style={{
              color: colors.accent.recording,
              fontFamily: font.bodySemibold,
              fontSize: size.bodyLg,
            }}
          >
            Sign out
          </Text>
        </Pressable>

        <Text
          style={{
            color: colors.text.muted,
            fontFamily: font.body,
            fontSize: size.small,
            textAlign: "center",
            marginTop: 16,
          }}
        >
          Sail club, home port and per-boat preferences are still on the
          web. Coming to mobile in a follow-up.
        </Text>
      </View>
    </View>
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
    flex: 1,
    paddingHorizontal: 24,
    paddingTop: 32,
  },
  identityCard: {
    alignItems: "center",
  },
  avatar: {
    width: 96,
    height: 96,
    borderRadius: 48,
  },
  signOut: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    height: 52,
    borderRadius: 26,
    borderWidth: StyleSheet.hairlineWidth,
  },
});
