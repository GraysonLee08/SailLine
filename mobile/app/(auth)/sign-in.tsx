// app/(auth)/sign-in.tsx — Google sign-in screen.
//
// Visual: minimal, light-themed, large logo wordmark. Sign-in is a one-
// time interaction; no need to over-design the chrome around it. The
// button uses the platform-native Pressable + theme colours rather than
// the stock <Button> (which can't be styled and looks like a 2014 web
// form on Android).

import {
  ActivityIndicator,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { useAuth } from "../../src/auth/AuthContext";
import { useTheme } from "../../src/theme/ThemeProvider";

export default function SignInScreen() {
  const { signIn, signingIn, error } = useAuth();
  const { colors, font, size } = useTheme();

  return (
    <ScrollView
      contentContainerStyle={[
        styles.container,
        { backgroundColor: colors.surface.page },
      ]}
    >
      <View style={styles.brand}>
        <Text
          style={[
            styles.wordmark,
            {
              color: colors.text.primary,
              fontFamily: font.displayBold,
              fontSize: size.hero,
            },
          ]}
        >
          SailLine
        </Text>
        <Text
          style={[
            styles.tag,
            { color: colors.text.secondary, fontFamily: font.body, fontSize: size.body },
          ]}
        >
          Race intelligence for the Great Lakes.
        </Text>
      </View>

      <View style={styles.cta}>
        {error ? (
          <Text
            style={[
              styles.error,
              { color: colors.accent.recording, fontFamily: font.body, fontSize: size.small },
            ]}
          >
            {error}
          </Text>
        ) : null}

        <Pressable
          onPress={signIn}
          disabled={signingIn}
          accessibilityRole="button"
          accessibilityLabel="Sign in with Google"
          style={({ pressed }) => [
            styles.button,
            {
              backgroundColor: pressed
                ? colors.accent.primaryPressed
                : colors.accent.primary,
              opacity: signingIn ? 0.7 : 1,
            },
          ]}
        >
          {signingIn ? (
            <ActivityIndicator color={colors.text.onAccent} />
          ) : (
            <Text
              style={[
                styles.buttonLabel,
                {
                  color: colors.text.onAccent,
                  fontFamily: font.bodySemibold,
                  fontSize: size.bodyLg,
                },
              ]}
            >
              Sign in with Google
            </Text>
          )}
        </Pressable>

        <Text
          style={[
            styles.note,
            { color: colors.text.muted, fontFamily: font.body, fontSize: size.small },
          ]}
        >
          Same Google account as the web app — your races, boats, and crew
          show up here automatically.
        </Text>
      </View>
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: {
    flexGrow: 1,
    paddingHorizontal: 28,
    paddingTop: 120,
    paddingBottom: 48,
    gap: 56,
  },
  brand: { gap: 8 },
  wordmark: { letterSpacing: -1 },
  tag: { lineHeight: 20 },
  cta: { gap: 16 },
  error: { lineHeight: 18 },
  button: {
    height: 52,
    borderRadius: 14,
    alignItems: "center",
    justifyContent: "center",
  },
  buttonLabel: { letterSpacing: 0.1 },
  note: { lineHeight: 18, marginTop: 8 },
});
