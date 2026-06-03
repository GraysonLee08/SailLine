// app/(app)/index.tsx — map home screen (Google-Maps-style).
//
// Full-bleed map canvas. Two bottom-sheet states layered on top:
//   * No race selected → RaceListSheet (browse).
//   * Race selected   → RaceDetailSheet (compute route + start recording).
//
// FABs sit top-right (under the compass) for locate-me + layers toggle.
// Wind barbs overlay the base map when toggled on.
//
// Lives in the (app) group so the auth gate in (app)/_layout.tsx
// guarantees a signed-in user before this screen mounts.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pressable, SafeAreaView, StyleSheet, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { router, useFocusEffect } from "expo-router";

import { useAuth } from "../../src/auth/AuthContext";
import { useRecorder } from "../../src/recorder/RecorderContext";
import {
  AppMenuSheet,
  type AppMenuSheetHandle,
} from "../../src/components/AppMenuSheet";
import { BetterRouteBanner } from "../../src/components/BetterRouteBanner";
import {
  MapCanvas,
  type MapCanvasHandle,
} from "../../src/components/MapCanvas";
import { MapFabs } from "../../src/components/MapFabs";
import { MapActionFabs } from "../../src/components/MapActionFabs";
import {
  RaceDetailSheet,
  type RaceDetailSheetHandle,
} from "../../src/components/RaceDetailSheet";
import { RaceListSheet } from "../../src/components/RaceListSheet";
import { WindBarbLayer } from "../../src/components/WindBarbLayer";
import { useRouteNotifications } from "../../src/hooks/useRouteNotifications";
import { useRouting } from "../../src/hooks/useRouting";
import { useAutoStartRecorder } from "../../src/recorder/useAutoStartRecorder";
import { useWeather } from "../../src/hooks/useWeather";
import { useOrientationSettings } from "../../src/hooks/useOrientationSettings";
import { useAutoRouteSetting } from "../../src/hooks/useAutoRouteSetting";
import { useTheme } from "../../src/theme/ThemeProvider";
import { computeBarbFeatures } from "../../src/lib/windBarbViewport";
import { listRaces } from "../../src/api/races";
import {
  baseRegionForPoint,
  marksCentroid,
  DEFAULT_BASE_REGION,
} from "@sailline/shared";
import type { Race } from "../../src/types";

export default function MapHomeScreen() {
  const { user, signOut } = useAuth();
  const { colors } = useTheme();
  const { selectedRace, setSelectedRace, recorder, startRecording } =
    useRecorder();

  // App menu sheet ref — opened by the hamburger button (top-left).
  // Mounts globally on the home screen so menu actions don't have to
  // wait for a route push first. 2026-06-03 B1.
  const menuRef = useRef<AppMenuSheetHandle>(null);

  // Race list
  const [races, setRaces] = useState<Race[] | null>(null);
  const [racesError, setRacesError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const loadRaces = useCallback(async () => {
    try {
      const data = await listRaces();
      setRaces(data);
      setRacesError(null);
    } catch (e) {
      setRacesError(e instanceof Error ? e.message : String(e));
      setRaces((prev) => prev ?? []);
    }
  }, []);

  useEffect(() => {
    void loadRaces();
  }, [loadRaces]);

  // Re-fetch on focus so newly-created or edited races show up when the
  // user pops back from /race-edit. Otherwise the screen keeps showing
  // its mount-time snapshot of the list.
  useFocusEffect(
    useCallback(() => {
      void loadRaces();
    }, [loadRaces]),
  );

  const onRefresh = useCallback(async () => {
    setRefreshing(true);
    try {
      await loadRaces();
    } finally {
      setRefreshing(false);
    }
  }, [loadRaces]);

  // Map ref for FAB-driven actions.
  const mapRef = useRef<MapCanvasHandle>(null);

  // Race detail sheet ref + expand state — fed to MapActionFabs so the
  // Minimize FAB can collapse the sheet and only render when expanded.
  // Phase 1 spec: 2026-05-29_mobile-ui-google-maps-mapping.md §4.
  const detailSheetRef = useRef<RaceDetailSheetHandle>(null);
  const [sheetExpanded, setSheetExpanded] = useState(false);

  // Resolve the wind region from the selected race's marks. Falls back to
  // CONUS so the user sees barbs immediately while browsing.
  const regionName = useMemo(() => {
    if (selectedRace) {
      const c = marksCentroid(selectedRace.marks);
      if (c) {
        const r = baseRegionForPoint(c.lat, c.lon);
        if (r) return r.name;
      }
    }
    return DEFAULT_BASE_REGION;
  }, [selectedRace]);

  // Wind data + viewport-driven barb features.
  const { grid: windGrid } = useWeather(regionName);
  const [viewport, setViewport] = useState<{
    zoom: number;
    bounds: { south: number; north: number; west: number; east: number };
    centerLat: number;
    centerLon: number;
    headingDeg: number;
  } | null>(null);
  const [windOn, setWindOn] = useState(true);

  const barbFeatures = useMemo(() => {
    if (!windGrid || !viewport || !windOn) return [];
    return computeBarbFeatures(viewport, windGrid, null);
  }, [windGrid, viewport, windOn]);

  // Routing.
  const routing = useRouting(selectedRace?.id ?? null);

  // SSE notifications — only active when a race is selected.
  const notifications = useRouteNotifications(selectedRace?.id ?? null);

  // Per-race orientation calibration + auto-route preference.
  const orientation = useOrientationSettings(selectedRace?.id ?? null);
  const autoRoute = useAutoRouteSetting(selectedRace?.id ?? null);

  // Auto-start arming (mirrors what the legacy RecorderScreen did). When
  // the race has a scheduled start, arms the foreground timer + the
  // OS-level T-6/T-5 fallbacks. The useEffect below detects the recorder
  // flipping to recording and replaces the route to /recording — so the
  // callback only needs to kick off startRecording, not navigate.
  const auto = useAutoStartRecorder({
    raceId: selectedRace?.id ?? null,
    startAtIso: selectedRace?.start_at ?? null,
    enabled: !!selectedRace,
    recording: recorder.recording,
    start: startRecording,
  });

  // When the user picks a race, fit the map to its bounds.
  const handleSelectRace = useCallback(
    (race: Race) => {
      if (!setSelectedRace(race)) return;
      mapRef.current?.fitToRace(race);
    },
    [setSelectedRace],
  );

  const handleCloseDetail = useCallback(() => {
    if (!setSelectedRace(null)) return;
  }, [setSelectedRace]);

  // Start recording. The useEffect below replaces the route to /recording
  // once recorder.recording flips to true, so we don't manually navigate
  // here — keeps the "what triggered the screen change" logic in one
  // place and avoids double-push when auto-start is also racing the same
  // recording flag.
  const handleStartRecording = useCallback(async () => {
    if (!selectedRace) return;
    await startRecording();
  }, [selectedRace, startRecording]);

  // If a recording is in progress and the user lands here (e.g., after a
  // crash recovery), bounce them to the recording chrome so they don't
  // see a "start recording" CTA on an active session.
  useEffect(() => {
    if (recorder.recording) router.replace("/recording");
  }, [recorder.recording]);

  return (
    <View style={styles.root}>
      <MapCanvas
        ref={mapRef}
        marks={selectedRace?.marks ?? []}
        route={routing.route}
        onCameraChanged={setViewport}
      >
        <WindBarbLayer
          features={barbFeatures}
          visible={windOn}
          zoom={viewport?.zoom}
        />
      </MapCanvas>

      <MapFabs
        onLocateMe={() => mapRef.current?.locateMe()}
        onToggleWind={() => setWindOn((v) => !v)}
        onToggleCompass={() => mapRef.current?.toggleCompass()}
        windOn={windOn}
        headingDeg={viewport?.headingDeg ?? 0}
      />

      {/* Hamburger menu button — opens AppMenuSheet (Races, Race
          Setup, Boats, Settings, Profile, Sign out). Sits top-left so
          it doesn't compete with the FAB cluster on the right. Inside
          SafeAreaView so it clears the notch / status bar. 2026-06-03
          B1. */}
      <SafeAreaView style={styles.menuButton} pointerEvents="box-none">
        <Pressable
          onPress={() => menuRef.current?.open()}
          accessibilityRole="button"
          accessibilityLabel="Open menu"
          style={({ pressed }) => [
            styles.menuChip,
            {
              backgroundColor: colors.surface.floating,
              borderColor: colors.border.hairline,
              opacity: pressed ? 0.85 : 1,
              shadowColor: colors.scrim.shadow,
            },
          ]}
        >
          <Ionicons name="menu" size={22} color={colors.text.primary} />
        </Pressable>
      </SafeAreaView>

      {selectedRace ? (
        <MapActionFabs
          onMinimize={() => detailSheetRef.current?.snapToPeek()}
          sheetExpanded={sheetExpanded}
        />
      ) : null}

      {selectedRace ? (
        <RaceDetailSheet
          ref={detailSheetRef}
          race={selectedRace}
          onClose={handleCloseDetail}
          onStart={handleStartRecording}
          onCompute={routing.compute}
          routeLoading={routing.loading}
          routeError={routing.error}
          routeMeta={routing.meta}
          routePending={routing.pending}
          autoStart={auto}
          betterRouteBanner={
            <BetterRouteBanner
              alternative={notifications.alternative}
              onAccept={() =>
                notifications.accept((feature) =>
                  routing.applyAlternative(feature),
                )
              }
              onDismiss={notifications.dismiss}
              autoAcceptSeconds={autoRoute.enabled ? 10 : 0}
            />
          }
          orientation={{
            phoneAxis: orientation.phoneAxis,
            onPhoneAxisChange: orientation.setPhoneAxis,
            calibration: orientation.calibration,
            onCaptureCalibration: orientation.setCalibration,
            enabled: true,
          }}
          autoRoute={{
            enabled: autoRoute.enabled,
            onToggle: autoRoute.setEnabled,
          }}
          onExpandedChange={setSheetExpanded}
        />
      ) : (
        <RaceListSheet
          races={races}
          error={racesError}
          refreshing={refreshing}
          onRefresh={onRefresh}
          onSelect={handleSelectRace}
          userEmail={user?.email ?? null}
          onSignOut={signOut}
        />
      )}

      {/* App menu — mounts at index=-1 (closed). The hamburger button
          above calls menuRef.current.open() to surface it. 2026-06-03
          B1. */}
      <AppMenuSheet
        ref={menuRef}
        userEmail={user?.email ?? null}
        onSignOut={signOut}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  menuButton: {
    position: "absolute",
    top: 8,
    left: 12,
  },
  menuChip: {
    width: 44,
    height: 44,
    borderRadius: 22,
    alignItems: "center",
    justifyContent: "center",
    borderWidth: StyleSheet.hairlineWidth,
    shadowOffset: { width: 0, height: 3 },
    shadowOpacity: 1,
    shadowRadius: 8,
    elevation: 3,
  },
});
