// app/(app)/recording.tsx — race-day recording chrome.
//
// Replaces the legacy RecorderScreen. Layout:
//   * Full-bleed map (marks + computed route + user position).
//   * Top-left: back chip + LIVE pill on the same row (left-aligned so they
//     sit below the clock, never under the status-bar battery icons).
//   * Top-right: the same FAB cluster home shows — compass (orientation),
//     layers (wind), and centre (locate-me). Same handlers, same component.
//   * GuidanceCard pinned above the bottom of the screen — the single
//     most-used surface during a race ("next mark / distance / on line").
//   * Stop button below the guidance card, big and red.
//   * Back chip — DISABLED while recording, as before.
//
// No bottom sheet here intentionally: a draggable sheet competes with the
// guidance card for the same screen real estate, and during a race the
// user wants every pixel showing the map + guidance. The sheet returns
// when recording stops.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pressable, SafeAreaView, StyleSheet, Text, View } from "react-native";
import { router } from "expo-router";
import { Ionicons } from "@expo/vector-icons";

import { MapCanvas, type MapCanvasHandle } from "../../src/components/MapCanvas";
import { GuidanceCard } from "../../src/components/GuidanceCard";
import { MapFabs } from "../../src/components/MapFabs";
import { MarkPassControls } from "../../src/components/MarkPassControls";
import { UploadStatusBadge } from "../../src/components/UploadStatusBadge";
import { WindBarbLayer } from "../../src/components/WindBarbLayer";
import { useMarkPasses } from "../../src/hooks/useMarkPasses";
import { useMissedMarkNotifier } from "../../src/hooks/useMissedMarkNotifier";
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

  // Layers FAB state — defaults ON during a race so wind context is visible
  // immediately. Home screen owns its own copy of this state independently.
  const [windOn, setWindOn] = useState(true);

  // MapCanvas imperative handle, used by:
  //   1. The mount effect below to fit the camera to the course on entry
  //      (avoids the ref-timing race in MapCanvas's internal fit effect,
  //      which fires before @rnmapbox/maps attaches its Camera ref on
  //      screens where `marks` are present from the very first render).
  //   2. The FAB cluster (locate-me, compass toggle).
  const mapRef = useRef<MapCanvasHandle>(null);

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

  // Mark-pass list — both auto-detected (server) and manual.
  // Always shown during recording per 2026-05-30 spec: each mark has a
  // "Pass" button so the sailor can confirm anything the detector misses.
  const markPasses = useMarkPasses({
    raceId: selectedRace?.id ?? null,
    recording: recorder.recording,
  });

  // Reuse the recorder's last GPS fix as the boat-position-at-tap.
  // Returns null if no fix yet so the API call omits lat/lon (server
  // falls back to the mark's nominal position).
  const getCurrentPosition = useCallback(() => {
    const p = recorder.lastPoint;
    if (!p) return null;
    return { lat: p.lat, lon: p.lon };
  }, [recorder.lastPoint]);

  // Fire a watch-actionable notification if the boat plausibly passed a
  // mark that the v3 detector missed (wide pass outside threshold).
  // Background hook — no UI; the notification owns the user surface.
  useMissedMarkNotifier({
    raceId: selectedRace?.id ?? null,
    recording: recorder.recording,
    marks: selectedRace?.marks ?? [],
    passes: markPasses.passes,
    lastPoint: recorder.lastPoint
      ? { lat: recorder.lastPoint.lat, lon: recorder.lastPoint.lon }
      : null,
  });

  // Bounce out if the user navigated here without a race. Use replace,
  // not push, so the back stack doesn't trap them on /recording.
  useEffect(() => {
    if (!selectedRace) router.replace("/");
  }, [selectedRace]);

  // Fit-to-course on mount. Mirrors home's behaviour when a race is
  // selected. Done via the imperative handle (not the declarative
  // initialBounds path in MapCanvas) because on /recording, marks are
  // present from the very first render — MapCanvas's internal fit effect
  // fires once before the Camera ref attaches, then never re-runs because
  // `initialBounds` reference doesn't change. Calling fitToRace from a
  // mount effect here runs AFTER the Camera mount, so the ref is live.
  useEffect(() => {
    if (!selectedRace) return;
    // setTimeout(0) defers past the first render commit, by which time the
    // Camera ref is reliably attached on both iOS and Android.
    const t = setTimeout(() => {
      mapRef.current?.fitToRace(selectedRace);
    }, 0);
    return () => clearTimeout(t);
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
        ref={mapRef}
        marks={selectedRace.marks}
        route={routing.route}
        onCameraChanged={setViewport}
      >
        <WindBarbLayer
          features={barbFeatures}
          visible={windOn}
          zoom={viewport?.zoom}
        />
      </MapCanvas>

      {/* Top-LEFT row: back chip + LIVE pill. Both sit inside the SafeArea
          so they never overlap the status bar / battery / signal icons. */}
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

        {/* Phase 3 — honest upload-health badge. Sits next to LIVE so
            the user can tell at a glance whether data is reaching the
            backend. LIVE means "we are recording"; this badge means
            "we are or are not uploading." */}
        {recorder.recording ? (
          <UploadStatusBadge
            status={recorder.uploadStatus}
            queueDepth={recorder.queueLength}
          />
        ) : null}
      </SafeAreaView>

      {/* Top-RIGHT: same FAB cluster the home screen uses. Compass toggles
          orientation (north / follow-heading), layers toggles wind barbs,
          centre re-centres on the user's position. */}
      <MapFabs
        onLocateMe={() => mapRef.current?.locateMe()}
        onToggleWind={() => setWindOn((v) => !v)}
        onToggleCompass={() => mapRef.current?.toggleCompass()}
        windOn={windOn}
        headingDeg={viewport?.headingDeg ?? 0}
      />

      {/* Bottom action stack: marks row + guidance card + Stop button.
          MarkPassControls sits above the GuidanceCard so the "what's
          confirmed" picture is immediately visible without obscuring
          the next-mark guidance. */}
      <SafeAreaView style={styles.bottomStack} pointerEvents="box-none">
        <MarkPassControls
          marks={selectedRace.marks}
          passes={markPasses.passes}
          disabled={!recorder.recording}
          getCurrentPosition={getCurrentPosition}
          onMarkPass={markPasses.markManualPass}
          pending={markPasses.loading && markPasses.passes.length === 0}
        />
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
    // Don't pin to `right` — the FAB cluster lives on the top-right edge
    // independently. Keeping the top bar left-anchored + flexShrink lets
    // long race names truncate cleanly without colliding with the FABs.
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    maxWidth: "70%",
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
    flexShrink: 1,
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
