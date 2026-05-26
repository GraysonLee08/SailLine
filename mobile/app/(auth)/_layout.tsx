// app/(auth)/_layout.tsx — sign-out shell.
//
// Redirects to the app's home if the user is already signed in. expo-router
// matches groups by parens; (auth) and (app) are sibling groups that don't
// add a URL segment. The redirect here ensures the user can't navigate
// backward to /sign-in once authed.

import { Redirect, Stack } from "expo-router";
import { ActivityIndicator, View } from "react-native";

import { useAuth } from "../../src/auth/AuthContext";
import { useTheme } from "../../src/theme/ThemeProvider";

export default function AuthLayout() {
  const { ready, user } = useAuth();
  const { colors } = useTheme();

  if (!ready) {
    return (
      <View
        style={{
          flex: 1,
          alignItems: "center",
          justifyContent: "center",
          backgroundColor: colors.surface.page,
        }}
      >
        <ActivityIndicator color={colors.accent.primary} />
      </View>
    );
  }
  if (user) return <Redirect href="/" />;

  return <Stack screenOptions={{ headerShown: false }} />;
}
