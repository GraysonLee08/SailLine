// AppMenuSheet.tsx — global app menu, rendered as a bottom sheet.
//
// The mobile analogue of the webapp's hamburger menu. Surfaces the
// non-map screens (Race Setup / Boats / Settings / Profile) that
// previously had no entry point on mobile.
//
// Implementation note — drawer vs. sheet:
//   The 2026-06-02 mobile-fixes plan called for a Drawer navigator
//   (`expo-router/drawer`). That would require installing
//   `@react-navigation/drawer` and a fresh `expo prebuild` + Gradle
//   build. Since we just stabilised local builds (C1) and are days from
//   a deployment, this commit deliberately ships the same navigation
//   surface as a Gorhom bottom sheet — Gorhom is already a dep, no
//   native config touched. A future commit can swap this for a real
//   Drawer without changing the user-facing menu items: same routes,
//   same labels. Tech debt flagged in the session summary.
//
// UX:
//   * Opens on hamburger tap from the home screen.
//   * Forwarded ref exposes `open()` / `close()` so the parent doesn't
//     manage `isOpen` state.
//   * Each row pushes its route via expo-router and closes the sheet.
//   * Sign-out row sits at the bottom in muted styling, mirroring the
//     RaceListSheet pattern.

import {
  forwardRef,
  useCallback,
  useImperativeHandle,
  useMemo,
  useRef,
} from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";
import BottomSheet, { BottomSheetBackdrop, BottomSheetView } from "@gorhom/bottom-sheet";
import { Ionicons } from "@expo/vector-icons";
import { router } from "expo-router";

import { useTheme } from "../theme/ThemeProvider";

export type AppMenuSheetHandle = {
  open: () => void;
  close: () => void;
};

type Props = {
  userEmail: string | null;
  onSignOut: () => void;
};

type IoniconName = React.ComponentProps<typeof Ionicons>["name"];

type MenuItem = {
  label: string;
  icon: IoniconName;
  route: string;
};

const ITEMS: MenuItem[] = [
  { label: "Races", icon: "list-outline", route: "/" },
  { label: "New race", icon: "add-circle-outline", route: "/race-edit" },
  { label: "Boats", icon: "boat-outline", route: "/boats" },
  { label: "Settings", icon: "settings-outline", route: "/settings" },
  { label: "Profile", icon: "person-outline", route: "/profile" },
];

export const AppMenuSheet = forwardRef<AppMenuSheetHandle, Props>(
  function AppMenuSheet({ userEmail, onSignOut }, ref) {
    const { colors, font, size } = useTheme();
    const sheetRef = useRef<BottomSheet>(null);

    useImperativeHandle(
      ref,
      () => ({
        open: () => sheetRef.current?.snapToIndex(0),
        close: () => sheetRef.current?.close(),
      }),
      [],
    );

    const handlePress = useCallback((route: string) => {
      sheetRef.current?.close();
      // Tiny defer so the sheet close animation starts before the
      // route push triggers a re-mount; otherwise on slower devices
      // the new screen appears on top of a half-closed sheet.
      setTimeout(() => router.push(route as never), 100);
    }, []);

    const handleSignOut = useCallback(() => {
      sheetRef.current?.close();
      setTimeout(() => {
        onSignOut();
      }, 100);
    }, [onSignOut]);

    const snapPoints = useMemo(() => ["55%"], []);

    return (
      <BottomSheet
        ref={sheetRef}
        index={-1}
        snapPoints={snapPoints}
        enablePanDownToClose
        backgroundStyle={{ backgroundColor: colors.surface.elevated }}
        handleIndicatorStyle={{ backgroundColor: colors.border.hairline }}
        backdropComponent={(props) => (
          <BottomSheetBackdrop
            {...props}
            appearsOnIndex={0}
            disappearsOnIndex={-1}
            opacity={0.4}
          />
        )}
      >
        <BottomSheetView style={styles.sheet}>
          <Text
            style={{
              color: colors.text.muted,
              fontFamily: font.body,
              fontSize: size.caption,
              letterSpacing: 0.8,
              textTransform: "uppercase",
              marginBottom: 8,
            }}
          >
            Menu
          </Text>

          {ITEMS.map((item) => (
            <Pressable
              key={item.route}
              onPress={() => handlePress(item.route)}
              accessibilityRole="button"
              accessibilityLabel={item.label}
              style={({ pressed }) => [
                styles.row,
                {
                  backgroundColor: pressed
                    ? colors.surface.page
                    : "transparent",
                  borderColor: colors.border.hairline,
                },
              ]}
            >
              <Ionicons
                name={item.icon}
                size={22}
                color={colors.text.primary}
                style={{ width: 28 }}
              />
              <Text
                style={{
                  color: colors.text.primary,
                  fontFamily: font.bodyMedium,
                  fontSize: size.bodyLg,
                  flex: 1,
                }}
              >
                {item.label}
              </Text>
              <Ionicons
                name="chevron-forward"
                size={18}
                color={colors.text.muted}
              />
            </Pressable>
          ))}

          <View style={[styles.spacer, { borderColor: colors.border.hairline }]} />

          {userEmail ? (
            <Text
              style={{
                color: colors.text.muted,
                fontFamily: font.body,
                fontSize: size.small,
                marginBottom: 4,
              }}
              numberOfLines={1}
            >
              Signed in as {userEmail}
            </Text>
          ) : null}
          <Pressable
            onPress={handleSignOut}
            accessibilityRole="button"
            accessibilityLabel="Sign out"
            style={({ pressed }) => [
              styles.row,
              {
                backgroundColor: pressed
                  ? colors.surface.page
                  : "transparent",
                borderColor: colors.border.hairline,
              },
            ]}
          >
            <Ionicons
              name="log-out-outline"
              size={22}
              color={colors.accent.recording}
              style={{ width: 28 }}
            />
            <Text
              style={{
                color: colors.accent.recording,
                fontFamily: font.bodyMedium,
                fontSize: size.bodyLg,
                flex: 1,
              }}
            >
              Sign out
            </Text>
          </Pressable>
        </BottomSheetView>
      </BottomSheet>
    );
  },
);

const styles = StyleSheet.create({
  sheet: {
    flex: 1,
    paddingHorizontal: 20,
    paddingTop: 6,
    paddingBottom: 24,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingVertical: 14,
    paddingHorizontal: 8,
    borderRadius: 12,
  },
  spacer: {
    marginVertical: 12,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
});
