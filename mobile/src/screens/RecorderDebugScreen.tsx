// RecorderDebugScreen.tsx — read-only diagnostic view of the recorder
// log + live stats.
//
// Phase 2 of the durable upload pipeline rework. v1 is intentionally
// minimal: a scrolling list of ring-buffer entries, plus a header
// summarizing the most recent successful flush and the queue depth.
//
// Access today: hidden — the route is /(app)/recorder-debug and is not
// linked from any visible menu. The user navigates to it via the route
// from a debug build of the home screen (Phase 3 will add a long-press
// gesture on the LIVE pill once the upload-status badge ships).
//
// Read source is the on-device AsyncStorage ring buffer for the
// currently-selected race id. If no race is selected, the screen
// renders an "empty" state.

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Pressable,
  RefreshControl,
  SafeAreaView,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { router } from "expo-router";

import { useRecorder } from "../recorder/RecorderContext";
import { loadLog, type RecorderLogEntry } from "../recorder/recorderLog";
import { useTheme } from "../theme/ThemeProvider";

function formatTs(ts: string): string {
  try {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return ts;
    return d.toISOString().slice(11, 23); // HH:MM:SS.mmm
  } catch {
    return ts;
  }
}

function entryColor(
  entry: RecorderLogEntry,
  colors: ReturnType<typeof useTheme>["colors"],
): string {
  if (entry.status === "error") return colors.accent.recording;
  if (entry.status === "ok") return colors.text.primary;
  return colors.text.muted;
}

export function RecorderDebugScreen() {
  const { colors, font, size } = useTheme();
  const { selectedRace, recorder } = useRecorder();

  const [log, setLog] = useState<RecorderLogEntry[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    if (!selectedRace) {
      setLog([]);
      setLoaded(true);
      return;
    }
    setRefreshing(true);
    try {
      const entries = await loadLog(selectedRace.id);
      // Newest first for the screen — humans read top-down for "what
      // just happened."
      setLog([...entries].reverse());
      setLoaded(true);
    } finally {
      setRefreshing(false);
    }
  }, [selectedRace]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Spacing is in literal pixels — the theme does not export a spacing
  // scale (see RaceEditScreen for the same pattern). font is family
  // names only; size is the font-size scale (caption / body / title…).
  const styles = useMemo(
    () =>
      StyleSheet.create({
        root: {
          flex: 1,
          backgroundColor: colors.surface.page,
        },
        header: {
          padding: 16,
          paddingTop: 24,
          borderBottomWidth: 1,
          borderBottomColor: colors.border.hairline,
          backgroundColor: colors.surface.sheet,
        },
        backRow: {
          flexDirection: "row",
          alignItems: "center",
          marginBottom: 12,
        },
        backButton: {
          color: colors.accent.primary,
          fontFamily: font.bodySemibold,
          fontSize: size.body,
        },
        title: {
          color: colors.text.primary,
          fontFamily: font.displaySemibold,
          fontSize: size.title,
          marginBottom: 4,
        },
        subtitle: {
          color: colors.text.muted,
          fontFamily: font.body,
          fontSize: size.caption,
          marginBottom: 12,
        },
        statRow: {
          flexDirection: "row",
          flexWrap: "wrap",
          gap: 12,
        },
        statBlock: {
          minWidth: 100,
        },
        statLabel: {
          color: colors.text.muted,
          fontFamily: font.body,
          fontSize: size.caption,
          textTransform: "uppercase",
          letterSpacing: 0.5,
        },
        statValue: {
          color: colors.text.primary,
          fontFamily: font.bodySemibold,
          fontSize: size.body,
          marginTop: 2,
        },
        listContent: {
          padding: 16,
          paddingBottom: 24,
        },
        emptyText: {
          color: colors.text.muted,
          fontFamily: font.body,
          fontSize: size.body,
          textAlign: "center",
          marginTop: 24,
        },
        entry: {
          paddingVertical: 8,
          borderBottomWidth: 1,
          borderBottomColor: colors.border.hairline,
        },
        entryHeader: {
          flexDirection: "row",
          alignItems: "baseline",
          gap: 8,
        },
        entryTime: {
          color: colors.text.muted,
          fontSize: size.caption,
          fontFamily: "monospace",
        },
        entryKind: {
          fontFamily: font.bodySemibold,
          fontSize: size.caption,
        },
        entryBody: {
          marginTop: 2,
          color: colors.text.primary,
          fontSize: size.caption,
          fontFamily: "monospace",
        },
      }),
    [colors, font, size],
  );

  return (
    <SafeAreaView style={styles.root}>
      <View style={styles.header}>
        <View style={styles.backRow}>
          <Pressable onPress={() => router.back()} hitSlop={12}>
            <Text style={styles.backButton}>{"< Back"}</Text>
          </Pressable>
        </View>
        <Text style={styles.title}>Recorder debug</Text>
        <Text style={styles.subtitle}>
          {selectedRace
            ? `Race: ${selectedRace.name}`
            : "No race selected — pick a race from the home screen first."}
        </Text>

        <View style={styles.statRow}>
          <View style={styles.statBlock}>
            <Text style={styles.statLabel}>Recording</Text>
            <Text style={styles.statValue}>
              {recorder.recording ? "yes" : "no"}
            </Text>
          </View>
          <View style={styles.statBlock}>
            <Text style={styles.statLabel}>Queue</Text>
            <Text style={styles.statValue}>{recorder.queueLength}</Text>
          </View>
          <View style={styles.statBlock}>
            <Text style={styles.statLabel}>Last fix</Text>
            <Text style={styles.statValue}>
              {recorder.lastPoint
                ? formatTs(recorder.lastPoint.recorded_at)
                : "—"}
            </Text>
          </View>
          <View style={styles.statBlock}>
            <Text style={styles.statLabel}>Log entries</Text>
            <Text style={styles.statValue}>{log.length}</Text>
          </View>
        </View>
      </View>

      <ScrollView
        contentContainerStyle={styles.listContent}
        refreshControl={
          <RefreshControl refreshing={refreshing} onRefresh={refresh} />
        }
      >
        {!loaded ? null : log.length === 0 ? (
          <Text style={styles.emptyText}>
            No log entries yet. The recorder writes here on every flush;
            on a healthy session you should see one per ~30 s.
          </Text>
        ) : (
          log.map((entry, idx) => (
            <View key={`${entry.ts}-${idx}`} style={styles.entry}>
              <View style={styles.entryHeader}>
                <Text style={styles.entryTime}>{formatTs(entry.ts)}</Text>
                <Text
                  style={[
                    styles.entryKind,
                    { color: entryColor(entry, colors) },
                  ]}
                >
                  {entry.kind}
                  {entry.status ? ` · ${entry.status}` : ""}
                  {entry.http_status != null ? ` · ${entry.http_status}` : ""}
                </Text>
              </View>
              <Text style={styles.entryBody}>
                {[
                  entry.batch_size != null
                    ? `batch=${entry.batch_size}`
                    : null,
                  entry.inserted != null ? `ins=${entry.inserted}` : null,
                  entry.queue_depth_after != null
                    ? `q=${entry.queue_depth_after}`
                    : null,
                  entry.duration_ms != null
                    ? `dur=${Math.round(entry.duration_ms)}ms`
                    : null,
                  entry.message ?? null,
                ]
                  .filter(Boolean)
                  .join(" · ") || "—"}
              </Text>
            </View>
          ))
        )}
      </ScrollView>
    </SafeAreaView>
  );
}
