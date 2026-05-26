// RacePickerScreen.tsx — pick a race to record.
//
// Replaces the Phase 1 "paste a race UUID" TextInput with a real list
// of the user's races, fetched from GET /api/races. Tap a row → calls
// onSelect(race) which the App-level state machine consumes to show
// RecorderScreen.
//
// Why a flat list, not a navigator: keeps the mobile app a single-file
// screen state machine (App.tsx) for now. expo-router / react-navigation
// land in Phase 2b when we also need Boats/Crew screens.
//
// Empty state pushes the user back to the web app to create a race —
// race creation on mobile is Phase 2b, and the race-day workflow
// expects you to have planned the race ahead of time anyway.

import { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator,
  Button,
  FlatList,
  RefreshControl,
  StyleSheet,
  Text,
  TouchableOpacity,
  View,
} from "react-native";

import { listRaces } from "../api/races";
import { formatRaceDate } from "../lib/formatRaceDate";
import type { Race } from "../types";

type Props = {
  /** Email of the current signed-in user — shown in the header. */
  userEmail: string | null;
  onSelect: (race: Race) => void;
  onSignOut: () => void;
};

export default function RacePickerScreen({
  userEmail,
  onSelect,
  onSignOut,
}: Props) {
  const [races, setRaces] = useState<Race[] | null>(null); // null = loading
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const data = await listRaces();
      setRaces(data);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      // Keep the previous list visible on refresh failure; only blank on
      // the initial load (when races is still null).
      setRaces((prev) => prev ?? []);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await load();
    } finally {
      setRefreshing(false);
    }
  }, [load]);

  // ── Loading ────────────────────────────────────────────────────────────
  if (races === null && !error) {
    return (
      <View style={[styles.container, styles.centered]}>
        <ActivityIndicator color="#8fb4c7" />
        <Text style={styles.muted}>Loading races…</Text>
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <View style={styles.header}>
        <View style={{ flex: 1 }}>
          <Text style={styles.title}>Races</Text>
          {userEmail ? <Text style={styles.subtitle}>{userEmail}</Text> : null}
        </View>
        <Button title="Sign out" color="#5e7d8c" onPress={onSignOut} />
      </View>

      {error ? (
        <View style={styles.errorBanner}>
          <Text style={styles.errorText}>Couldn't load races: {error}</Text>
        </View>
      ) : null}

      {races && races.length === 0 ? (
        <View style={styles.empty}>
          <Text style={styles.emptyTitle}>No races yet.</Text>
          <Text style={styles.emptyHint}>
            Plan your race on the web app first
            (sailline.app), then it'll show up here.
          </Text>
        </View>
      ) : (
        <FlatList
          data={races ?? []}
          keyExtractor={(r) => r.id}
          contentContainerStyle={styles.list}
          renderItem={({ item }) => (
            <RaceRow race={item} onPress={() => onSelect(item)} />
          )}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={onRefresh}
              tintColor="#8fb4c7"
            />
          }
        />
      )}
    </View>
  );
}

// ── Row ────────────────────────────────────────────────────────────────
function RaceRow({ race, onPress }: { race: Race; onPress: () => void }) {
  const raced =
    !!race.stats_available || (race.mark_passes?.length ?? 0) > 0;

  return (
    <TouchableOpacity
      style={styles.row}
      onPress={onPress}
      accessibilityLabel={`Open ${race.name}`}
    >
      <View style={styles.rowMain}>
        <View style={styles.rowHeader}>
          <Text style={styles.raceName} numberOfLines={1}>
            {race.name}
          </Text>
          {raced ? (
            <View style={styles.racedPill}>
              <Text style={styles.racedPillText}>RACED</Text>
            </View>
          ) : null}
        </View>
        <Text style={styles.rowMeta}>{formatRaceDate(race.start_at)}</Text>
        <Text style={styles.rowMeta}>
          {race.mode} · {race.boat_class} · {race.marks.length}{" "}
          {race.marks.length === 1 ? "mark" : "marks"}
        </Text>
      </View>
      <Text style={styles.chevron}>›</Text>
    </TouchableOpacity>
  );
}

// ── Styles ─────────────────────────────────────────────────────────────
const styles = StyleSheet.create({
  container: {
    flex: 1,
    backgroundColor: "#0b1f2a",
    paddingTop: 72,
    paddingHorizontal: 24,
  },
  centered: { alignItems: "center", justifyContent: "center", gap: 12 },
  header: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    marginBottom: 16,
  },
  title: { color: "#f5f7fa", fontSize: 26, fontWeight: "700" },
  subtitle: { color: "#8fb4c7", fontSize: 13, marginTop: 2 },
  muted: { color: "#8fb4c7", fontSize: 14 },

  errorBanner: {
    backgroundColor: "rgba(214, 59, 31, 0.15)",
    borderRadius: 10,
    padding: 12,
    marginBottom: 12,
  },
  errorText: { color: "#e08a8a", fontSize: 13 },

  empty: {
    flex: 1,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 24,
  },
  emptyTitle: {
    color: "#f5f7fa",
    fontSize: 18,
    fontWeight: "600",
    marginBottom: 8,
  },
  emptyHint: {
    color: "#8fb4c7",
    fontSize: 14,
    textAlign: "center",
    lineHeight: 20,
  },

  list: { paddingBottom: 32, gap: 10 },

  row: {
    backgroundColor: "#13303f",
    borderRadius: 12,
    padding: 16,
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  rowMain: { flex: 1 },
  rowHeader: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    marginBottom: 6,
  },
  raceName: {
    color: "#f5f7fa",
    fontSize: 17,
    fontWeight: "600",
    flexShrink: 1,
  },
  racedPill: {
    backgroundColor: "#1d3a32",
    paddingHorizontal: 8,
    paddingVertical: 2,
    borderRadius: 999,
  },
  racedPillText: {
    color: "#5dcaa5",
    fontSize: 10,
    fontWeight: "700",
    letterSpacing: 0.5,
  },
  rowMeta: { color: "#8fb4c7", fontSize: 13, marginTop: 2 },
  chevron: { color: "#5e7d8c", fontSize: 28, fontWeight: "300" },
});
