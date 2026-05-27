// app/(app)/recording.tsx — race-day recording chrome.
//
// Replaces the legacy RecorderScreen. Layout:
//   * Full-bleed map (marks + computed route + user position).
//   * GuidanceCard pinned above the bottom of the screen — the single
//     most-used surface during a race ("next mark / distance / on line").
//   * Stop button below the guidance card, big and red.
//   * Top-left back button — DISABLED while recording, as before.
//
// No bottom sheet here intentionally: a draggable sheet competes with the
// guidance card for the same screen real estate, and during a race the
// user wants every pixel showing the map + guidance. The sheet returns
// when recording stops.

import { useCallback, useEffect, useMemo, useState } from "react";
import { Pressable, SafeAreaView, StyleSheet, Text, View } from "react-native";
import { router } from "expo-router";
import { Ionicons } from "@expo/vector-icons";

import { MapCanvas } from "../../src/components/MapCanvas";
import { GuidanceCard } from "../../src/components/GuidanceCard";
import { WindBarbLayer } from "../../src/components/WindBarbLayer";
import { useNextMarkGuidance } from "../../src/hooks/useNextMarkGuidance";
import { useWeather } from "../../src/hooks/useWeather";
import { useRouting } from "../../src/hooks/useRouting";
import { useRecorder } from "../../src/recorder/RecorderContext";
import { useTheme } from "../../src/theme/ThemeProvider";
import { computeBarbFeatures } from "../../src/lib/windBarbViewport";
import {
  baseRegionForPoint,
  marksCentroid,
  DEFAULT_BASE_REGION,
} from "@sailline/shared";

export default function RecordingScreen() {
  const { selectedRace, recorder } = useRecorder();
  const { colors, font, size } = useTheme();

  // Hooks MUST run unconditionally — bounce-out logic is below as an
  // effect, not an early return that skips the hook calls below.
  const regionName = useMemo(() => {
    if (!selectedRace) return DEFAULT_BASE_REGION;
    const c = marksCentroid(selectedRace.marks);
    if (c) {
      const r = baseRegionForPoint(c.lat, c.lon);
      if (r) return r.name;
    }
    return DEFAULT_BASE_REGION;
  }, [selectedRace]);

  const { grid: windGrid } = useWeather(regionName);
  const [viewport, setViewport] = useState<{
    zoom: number;
    bounds: { south: number; north: number; west: number; east: number };
    centerLat: number;
    centerLon: number;
    headingDeg: number;
  } | null>(null);

  const barbFeatures = useMemo(() => {
    if (!windGrid || !viewport) return [];
    return computeBarbFeatures(viewport, windGrid, null);
  }, [windGrid, viewport]);

  // We don't compute a fresh route in the racing screen — the home screen
  // owns the compute action. We DO render any route that was computed
  // earlier so the user can compare actual track to plan; we fetch it
  // from the routing hook scoped to this race id, which will return the
  // cached route from Redis (same TTL).
  const routing = useRouting(selectedRace?.id ?? null);

  const guidance = useNextMarkGuidance({
    race: selectedRace,
    points: recorder.points,
    lastPoint: recorder.lastPoint,
  });

  // Bounce out if the user navigated here without a race. Use replace,
  // not push, so the back stack doesn't trap them on /recording.
  useEffect(() => {
    if (!selectedRace) router.replace("/");
  }, [selectedRace]);

  if (!selectedRace) return null;

  const handleStop = useCallback(async () => {
    await recorder.stop();
    // After stopping, drop back to the map home so the race detail sheet
    // re-appears with the same race still selected (handy for reviewing
    // what just happened).
    router.replace("/");
  }, [recorder]);

  const handleBack = useCallback(() => {
    if (recorder.recording) return;
    router.back();
  }, [recorder.recording]);

  return (
    <View style={styles.root}>
      <MapCanvas
        marks={selectedRace.marks}
        route={routing.route}
        onCameraChanged={setViewport}
      >
        <WindBarbLayer features={barbFeatures} visible={true} />
      </MapCanvas>

      {/* Top-left back chip. Disabled while recording. */}
      <SafeAreaView style={styles.topBar} pointerEvents="box-none">
        <Pressable
          onPress={handleBack}
          disabled={recorder.recording}
          accessibilityLabel="Back"
          style={({ pressed }) => [
            styles.backChip,
            {
              backgroundColor: colors.surface.floating,
              borderColor: colors.border.hairline,
              opacity: recorder.recording ? 0.5 : pressed ? 0.85 : 1,
              shadowColor: colors.scrim.shadow,
            },
          ]}
        >
          <Ionicons
            name="chevron-back"
            size={20}
            color={colors.text.primary}
          />
          <Text
            style={{
              color: colors.text.primary,
              fontFamily: font.bodyMedium,
              fontSize: size.small,
            }}
            numberOfLines={1}
          >
            {selectedRace.name}
          </Text>
        </Pressable>

        {recorder.recording ? (
          <View
            style={[
              styles.liveChip,
              {
                backgroundColor: `${colors.accent.recording}26`,
                borderColor: `${colors.accent.recording}55`,
              },
            ]}
          >
            <View
              style={[
                styles.liveDot,
                { backgroundColor: colors.accent.recording },
              ]}
            />
            <Text
              style={{
                color: colors.accent.recording,
                fontFamily: font.bodyBold,
                fontSize: size.caption,
                letterSpacing: 0.6,
              }}
            >
              LIVE
            </Text>
          </View>
        ) : null}
      </SafeAreaView>

      {/* Bottom action stack: guidance card + Stop button */}
      <SafeAreaView style={styles.bottomStack} pointerEvents="box-none">
        <GuidanceCard
          guidance={guidance}
          totalMarks={selectedRace.marks.length}
          speedKt={recorder.lastPoint?.speed_kts ?? null}
          headingDeg={recorder.lastPoint?.heading_deg ?? null}
        />

        {recorder.recording ? (
          <Pressable
            onPress={handleStop}
            accessibilityLabel="Stop recording"
            style={({ pressed }) => [
              styles.stop,
              {
                backgroundColor: pressed
                  ? colors.accent.stop
                  : colors.accent.recording,
              },
            ]}
          >
            <Ionicons name="stop" size={20} color={colors.text.onAccent} />
            <Text
              style={{
                color: colors.text.onAccent,
                fontFamily: font.bodySemibold,
                fontSize: size.bodyLg,
              }}
            >
              Stop
            </Text>
          </Pressable>
        ) : (
          <Pressable
            onPress={() => router.replace("/")}
            accessibilityLabel="Back to map"
            style={({ pressed }) => [
              styles.stop,
              {
                backgroundColor: pressed
                  ? colors.accent.primaryPressed
                  : colors.accent.primary,
              },
            ]}
          >
            <Ionicons name="map" size={20} color={colors.text.onAccent} />
            <Text
              style={{
                color: colors.text.onAccent,
                fontFamily: font.bodySemibold,
                fontSize: size.bodyLg,
              }}
            >
              Back to map
            </Text>
          </Pressable>
        )}

        {recorder.error ? (
          <Text
            style={{
              color: colors.accent.recording,
              fontFamily: font.body,
              fontSize: size.small,
              textAlign: "center",
              marginTop: 4,
            }}
          >
            {recorder.error}
          </Text>
        ) : null}
      </SafeAreaView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  topBar: {
    position: "absolute",
    top: 8,
    left: 12,
    right: 12,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    gap: 12,
  },
  backChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingLeft: 8,
    paddingRight: 14,
    height: 38,
    borderRadius: 19,
    borderWidth: StyleSheet.hairlineWidth,
    maxWidth: "70%",
    shadowOffset: { width: 0, height: 3 },
    shadowOpacity: 1,
    shadowRadius: 8,
    elevation: 3,
  },
  liveChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 10,
    height: 28,
    borderRadius: 14,
    borderWidth: StyleSheet.hairlineWidth,
  },
  liveDot: {
    width: 8,
    height: 8,
    borderRadius: 4,
  },
  bottomStack: {
    position: "absolute",
    left: 12,
    right: 12,
    bottom: 12,
    gap: 12,
  },
  stop: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    height: 56,
    borderRadius: 28,
  },
});
