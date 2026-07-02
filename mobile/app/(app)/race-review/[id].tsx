// app/(app)/race-review/[id].tsx — post-race Review screen (fallback).
//
// The map-first Debrief screen (app/(app)/debrief/[id].tsx) supersedes
// this as the default destination for finished races — see the branch in
// app/(app)/index.tsx::handleSelectRace. This stays routable as a
// text-only fallback for old links and as a lighter view when no map is
// wanted. The stats + AI recap content lives in the shared
// RaceReviewSections component (extracted 2026-07-02 for the debrief);
// this screen is just the header + scroll chrome around it.

import { Pressable, ScrollView, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { router, useLocalSearchParams } from "expo-router";
import { Ionicons } from "@expo/vector-icons";

import { RaceReviewSections } from "../../../src/components/RaceReviewSections";
import { formatRaceDate } from "../../../src/lib/formatRaceDate";
import { useRaceStats } from "../../../src/hooks/useRaceStats";
import { useTheme } from "../../../src/theme/ThemeProvider";

export default function RaceReviewScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { colors, font, size } = useTheme();
  const { data, phase, error, refresh } = useRaceStats(id ?? null);

  return (
    <View style={[styles.root, { backgroundColor: colors.surface.page }]}>
      <SafeAreaView edges={["top"]} style={styles.headerWrap}>
        <View style={styles.header}>
          <Pressable
            onPress={() => router.back()}
            accessibilityLabel="Back"
            hitSlop={10}
            style={styles.backBtn}
          >
            <Ionicons name="chevron-back" size={24} color={colors.text.primary} />
          </Pressable>
          <View style={{ flex: 1 }}>
            <Text
              style={{
                color: colors.text.primary,
                fontFamily: font.displaySemibold,
                fontSize: size.title,
              }}
              numberOfLines={1}
            >
              {data?.name ?? "Race review"}
            </Text>
            <Text
              style={{
                color: colors.text.muted,
                fontFamily: font.body,
                fontSize: size.small,
                marginTop: 1,
              }}
              numberOfLines={1}
            >
              {[
                data?.start_at ? formatRaceDate(data.start_at) : null,
                data?.boat_class,
              ]
                .filter(Boolean)
                .join(" · ")}
            </Text>
          </View>
        </View>
      </SafeAreaView>

      <ScrollView
        contentContainerStyle={styles.scroll}
        showsVerticalScrollIndicator={false}
      >
        <RaceReviewSections
          data={data}
          phase={phase}
          error={error}
          refresh={refresh}
        />
        <View style={{ height: 24 }} />
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  headerWrap: { paddingHorizontal: 0 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 14,
    paddingTop: 4,
    paddingBottom: 10,
  },
  backBtn: { padding: 2, marginLeft: -2 },
  scroll: { paddingHorizontal: 16, paddingBottom: 8 },
});
