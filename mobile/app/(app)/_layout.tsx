// app/(app)/_layout.tsx — authed shell.
//
// Redirects unauthed users back to /sign-in. The (app) group contains the
// map home and the recording screen; both rely on the recorder context
// already mounted at the root layer (so the recorder survives navigation
// between them).

import { Redirect, Stack } from "expo-router";
import { ActivityIndicator, View } from "react-native";

import { useAuth } from "../../src/auth/AuthContext";
import { useTheme } from "../../src/theme/ThemeProvider";

export default function AppLayout() {
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
  if (!user) return <Redirect href="/sign-in" />;

  return (
    <Stack
      screenOptions={{
        headerShown: false,
        animation: "slide_from_right",
        contentStyle: { backgroundColor: colors.surface.page },
      }}
    />
  );
}
