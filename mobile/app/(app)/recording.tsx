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
import { Pressable, StyleSheet, Text, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { router } from "expo-router";
import { Ionicons } from "@expo/vector-icons";

import { ActualRouteLayer } from "../../src/components/ActualRouteLayer";
import { BetterRouteBanner } from "../../src/components/BetterRouteBanner";
import { LayersPanel } from "../../src/components/LayersPanel";
import { MapCanvas, type MapCanvasHandle } from "../../src/components/MapCanvas";
import { GuidanceCard } from "../../src/components/GuidanceCard";
import { MapFabs } from "../../src/components/MapFabs";
import { UploadStatusBadge } from "../../src/components/UploadStatusBadge";
import { WindBarbLayer } from "../../src/components/WindBarbLayer";
import { useAutoPassSetting } from "../../src/hooks/useAutoPassSetting";
import { useAutoRouteSetting } from "../../src/hooks/useAutoRouteSetting";
import { useLayerSettings } from "../../src/hooks/useLayerSettings";
import { useMarkPasses } from "../../src/hooks/useMarkPasses";
import { useMissedMarkNotifier } from "../../src/hooks/useMissedMarkNotifier";
import { useNextMarkGuidance } from "../../src/hooks/useNextMarkGuidance";
import { useWeather } from "../../src/hooks/useWeather";
import { useRoute } from "../../src/routing/RoutingContext";
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

  // Layer visibility (2026-06-04 item 6) — shared with home via
  // AsyncStorage. The Layers FAB opens LayersPanel which writes back.
  // Recording-screen-specific layers (actualRoute) come from the same
  // hook so the user's preference survives across launches and screens.
  const layers = useLayerSettings();
  const [layersOpen, setLayersOpen] = useState(false);

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

  // Shared routing state (RoutingProvider). The home screen normally owns
  // the compute action and the resulting `route` survives navigation here,
  // so we render exactly the plan the user saw before tapping Start. The
  // same instance also carries the better-route SSE stream, so "faster
  // route" alerts surface on this screen too. A mount-effect below covers
  // the auto-start / crash-recovery paths where the user reaches /recording
  // without having computed on home.
  const routing = useRoute();

  // Auto-route preference (per race) drives the banner's auto-accept
  // countdown — same contract as the home screen.
  const autoRoute = useAutoRouteSetting(selectedRace?.id ?? null);

  // Compute-on-mount fallback. If we landed here without a computed route
  // (auto-start armed the recorder and replaced the route to /recording, or
  // the app crash-recovered into an active session), fetch it once. The
  // backend POST /api/routing/compute is idempotent + Redis-cached, so this
  // returns the same route immediately when one already exists. Guards keep
  // it from re-firing while a request is in flight, after a 425 "too early"
  // result, or after an error.
  useEffect(() => {
    if (!selectedRace) return;
    if (
      routing.route ||
      routing.loading ||
      routing.pending ||
      routing.error
    ) {
      return;
    }
    void routing.compute();
  }, [
    selectedRace,
    routing.route,
    routing.loading,
    routing.pending,
    routing.error,
    routing.compute,
  ]);

  const guidance = useNextMarkGuidance({
    race: selectedRace,
    points: recorder.points,
    lastPoint: recorder.lastPoint,
  });

  // Auto-detected mark passes (server-authoritative, read-only). Drives
  // the missed-mark notifier's "next expected mark" logic. There is no
  // in-race mark UI and no manual tap-to-pass — the detector is the sole
  // writer (manual-pass path removed 2026-06-08).
  const markPasses = useMarkPasses({
    raceId: selectedRace?.id ?? null,
    recording: recorder.recording,
  });

  // Global auto-pass preference (B2 — 2026-06-03). Default ON; users can
  // disable in the Settings screen if they prefer to mark passes only
  // by hand. When OFF, useMissedMarkNotifier still mounts (rules of
  // hooks) but its `enabled` flag short-circuits the alert path.
  const autoPass = useAutoPassSetting();

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
    enabled: autoPass.enabled,
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
        route={layers.route ? routing.route : null}
        onCameraChanged={setViewport}
      >
        <WindBarbLayer
          features={barbFeatures}
          visible={layers.wind}
          zoom={viewport?.zoom}
        />
        {/* Live breadcrumb of fixes captured this session. Hidden when
            no points yet OR the user toggled it off. 2026-06-03 B3. */}
        <ActualRouteLayer
          points={recorder.points}
          visible={layers.actualRoute && recorder.points.length > 0}
        />
      </MapCanvas>

      {/* Top-LEFT row: back chip + LIVE pill. Both sit inside the SafeArea
          so they never overlap the status bar / battery / signal icons.
          SafeAreaView from react-native-safe-area-context (not the base
          react-native one — that's iOS-only and let the chip render
          behind the Android clock; 2026-06-04 item 3). edges=["top"]
          so the bottom inset doesn't push the row down. */}
      <SafeAreaView
        edges={["top"]}
        style={styles.topBar}
        pointerEvents="box-none"
      >
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

        {/* Phase 3 — honest upload-health badge. Only renders when
            uploads need user attention (BUFFER / STALL / OFFLINE).
            When status === "live" the dedicated LIVE chip above is
            already saying "all is well," so showing the badge as a
            second "LIVE" pill is redundant and was reported as a
            duplicate by the user (2026-06-03). The badge re-appears
            the moment something is wrong, which IS when we want it
            visible. */}
        {recorder.recording && recorder.uploadStatus !== "live" ? (
          <UploadStatusBadge
            status={recorder.uploadStatus}
            queueDepth={recorder.queueLength}
          />
        ) : null}
      </SafeAreaView>

      {/* Top-RIGHT: same FAB cluster the home screen uses. Compass
          toggles orientation (north / follow-heading), layers opens the
          LayersPanel (Route / Actual route / Wind / Waves), centre
          re-centres on the user's position. */}
      <MapFabs
        onLocateMe={() => mapRef.current?.locateMe()}
        onOpenLayers={() => setLayersOpen(true)}
        onToggleCompass={() => mapRef.current?.toggleCompass()}
        headingDeg={viewport?.headingDeg ?? 0}
        anyLayerOn={layers.route || layers.actualRoute || layers.wind}
      />

      <LayersPanel
        visible={layersOpen}
        onClose={() => setLayersOpen(false)}
        layers={{
          route: layers.route,
          actualRoute: layers.actualRoute,
          wind: layers.wind,
          waves: layers.waves,
        }}
        onToggleLayer={layers.setLayer}
        /* Recording screen has a live track to render → show the
           Actual route toggle. */
        showActualRouteRow
      />

      {/* Bottom action stack: guidance card + Stop button.
          edges=["bottom"] keeps the stack above the home-indicator /
          nav bar without padding the top. */}
      <SafeAreaView
        edges={["bottom"]}
        style={styles.bottomStack}
        pointerEvents="box-none"
      >
        {/* Faster-route prompt. Same component + auto-accept contract as
            home; here it sits at the top of the bottom stack (this screen
            has no bottom sheet to tuck it into). Renders nothing unless an
            alternative is live. */}
        <BetterRouteBanner
          alternative={routing.alternative}
          onAccept={() => routing.acceptAlternative()}
          onDismiss={routing.dismissAlternative}
          autoAcceptSeconds={autoRoute.enabled ? 10 : 0}
        />
        <GuidanceCard
          guidance={guidance}
          totalMarks={selectedRace.marks.length}
          speedKt={recorder.lastPoint?.speed_kts ?? null}
          headingDeg={recorder.lastPoint?.heading_deg ?? null}
          /* "Waiting for GPS" mode: actively recording but the last
             fix lacks speed (stationary on a Couch or the SDK hasn't
             populated speed yet). Suppresses the bare em-dash so the
             user knows the recorder is alive. 2026-06-03 A4. */
          awaitingGps={
            recorder.recording &&
            (recorder.lastPoint == null ||
              recorder.lastPoint.speed_kts == null)
          }
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
