// RaceListSheet.tsx — bottom sheet that lists the user's races.
//
// Three snap points:
//   peek (15%)  — handle + a single header line ("Your races").
//   half (45%)  — visible chunk of the list, the default "I'm browsing" state.
//   full (90%)  — pulled all the way up, full scroll.
//
// Tap a row → calls onSelect(race). The parent uses that to fit the map
// to the race's bounds and surface "Plan route" / "Start recording"
// actions in a follow-up sheet state (see app/(app)/index.tsx).
//
// Empty state pushes the user to the web app — race creation on mobile
// is Phase 2b. No filter/sort/search for now; if the user accumulates
// 20+ races we'll add it.

import { useCallback, useEffect, useMemo, useRef } from "react";
import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from "react-native";
import BottomSheet, {
  BottomSheetFlatList,
  BottomSheetView,
} from "@gorhom/bottom-sheet";
import { Ionicons } from "@expo/vector-icons";

import { formatRaceDate } from "../lib/formatRaceDate";
import { useTheme } from "../theme/ThemeProvider";
import type { Race } from "../types";

type Props = {
  races: Race[] | null; // null = loading
  error: string | null;
  refreshing: boolean;
  onRefresh: () => void;
  onSelect: (race: Race) => void;
  userEmail: string | null;
  onSignOut: () => void;
};

const SNAP_POINTS = ["15%", "45%", "90%"] as const;

export function RaceListSheet({
  races,
  error,
  refreshing,
  onRefresh,
  onSelect,
  userEmail,
  onSignOut,
}: Props) {
  const { colors, font, size } = useTheme();
  const sheetRef = useRef<BottomSheet>(null);

  // Bring the sheet to "half" once races have loaded — feels like Google
  // Maps surfacing nearby results once they're ready.
  useEffect(() => {
    if (races && races.length > 0) {
      sheetRef.current?.snapToIndex(1);
    }
  }, [races]);

  const handleIndicator = useMemo(
    () => ({ backgroundColor: colors.scrim.handle }),
    [colors.scrim.handle],
  );
  const sheetBg = useMemo(
    () => ({ backgroundColor: colors.surface.sheet }),
    [colors.surface.sheet],
  );

  const renderRow = useCallback(
    ({ item }: { item: Race }) => (
      <RaceRow race={item} onPress={() => onSelect(item)} />
    ),
    [onSelect],
  );

  return (
    <BottomSheet
      ref={sheetRef}
      index={0}
      snapPoints={SNAP_POINTS}
      handleIndicatorStyle={handleIndicator}
      backgroundStyle={sheetBg}
      enableDynamicSizing={false}
      enableOverDrag={false}
    >
      <BottomSheetView
        style={[styles.header, { borderBottomColor: colors.border.hairline }]}
      >
        <View style={{ flex: 1 }}>
          <Text
            style={[
              styles.title,
              {
                color: colors.text.primary,
                fontFamily: font.displaySemibold,
                fontSize: size.title,
              },
            ]}
          >
            Your races
          </Text>
          {userEmail ? (
            <Text
              style={{
                color: colors.text.muted,
                fontFamily: font.body,
                fontSize: size.small,
                marginTop: 2,
              }}
              numberOfLines={1}
            >
              {userEmail}
            </Text>
          ) : null}
        </View>
        <Pressable
          onPress={onSignOut}
          accessibilityLabel="Sign out"
          hitSlop={10}
        >
          <Ionicons name="log-out-outline" size={22} color={colors.text.muted} />
        </Pressable>
      </BottomSheetView>

      {error ? (
        <View
          style={[
            styles.errorBanner,
            { backgroundColor: `${colors.accent.recording}22` },
          ]}
        >
          <Text
            style={{
              color: colors.accent.recording,
              fontFamily: font.body,
              fontSize: size.small,
            }}
          >
            Couldn't load races: {error}
          </Text>
        </View>
      ) : null}

      {races === null ? (
        <View style={styles.centerFill}>
          <ActivityIndicator color={colors.accent.primary} />
        </View>
      ) : races.length === 0 ? (
        <View style={styles.empty}>
          <Text
            style={{
              color: colors.text.primary,
              fontFamily: font.displaySemibold,
              fontSize: size.subtitle,
              marginBottom: 8,
            }}
          >
            No races yet.
          </Text>
          <Text
            style={{
              color: colors.text.muted,
              fontFamily: font.body,
              fontSize: size.body,
              lineHeight: 20,
              textAlign: "center",
            }}
          >
            Plan a race on the web app (sailline.app), then it'll show up
            here ready to record.
          </Text>
        </View>
      ) : (
        <BottomSheetFlatList
          data={races}
          keyExtractor={(r) => r.id}
          renderItem={renderRow}
          contentContainerStyle={styles.list}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={onRefresh}
              tintColor={colors.accent.primary}
            />
          }
        />
      )}
    </BottomSheet>
  );
}

function RaceRow({ race, onPress }: { race: Race; onPress: () => void }) {
  const { colors, font, size } = useTheme();
  const raced =
    !!race.stats_available || (race.mark_passes?.length ?? 0) > 0;

  return (
    <Pressable
      onPress={onPress}
      accessibilityLabel={`Open ${race.name}`}
      style={({ pressed }) => [
        styles.row,
        {
          backgroundColor: pressed
            ? colors.surface.elevated
            : colors.surface.sheet,
          borderBottomColor: colors.border.hairline,
        },
      ]}
    >
      <View style={styles.rowMain}>
        <View style={styles.rowHeader}>
          <Text
            style={[
              styles.raceName,
              {
                color: colors.text.primary,
                fontFamily: font.displaySemibold,
                fontSize: size.bodyLg,
              },
            ]}
            numberOfLines={1}
          >
            {race.name}
          </Text>
          {raced ? (
            <View
              style={[
                styles.racedPill,
                { backgroundColor: `${colors.accent.success}26` },
              ]}
            >
              <Text
                style={{
                  color: colors.accent.success,
                  fontFamily: font.bodyBold,
                  fontSize: 10,
                  letterSpacing: 0.5,
                }}
              >
                RACED
              </Text>
            </View>
          ) : null}
        </View>
        <Text
          style={{
            color: colors.text.secondary,
            fontFamily: font.body,
            fontSize: size.small,
            marginTop: 2,
          }}
        >
          {formatRaceDate(race.start_at)}
        </Text>
        <Text
          style={{
            color: colors.text.muted,
            fontFamily: font.body,
            fontSize: size.small,
            marginTop: 1,
          }}
        >
          {race.mode} · {race.boat_class} · {race.marks.length}{" "}
          {race.marks.length === 1 ? "mark" : "marks"}
        </Text>
      </View>
      <Ionicons name="chevron-forward" size={20} color={colors.text.muted} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  header: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 20,
    paddingTop: 4,
    paddingBottom: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  title: { letterSpacing: -0.3 },
  errorBanner: {
    marginHorizontal: 20,
    marginTop: 12,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 8,
  },
  centerFill: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingVertical: 40,
  },
  empty: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 32,
    paddingVertical: 40,
  },
  list: { paddingBottom: 40 },
  row: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 20,
    paddingVertical: 14,
    gap: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  rowMain: { flex: 1 },
  rowHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  raceName: { flexShrink: 1, letterSpacing: -0.2 },
  racedPill: {
    paddingHorizontal: 7,
    paddingVertical: 2,
    borderRadius: 999,
  },
});
