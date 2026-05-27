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
import { StyleSheet, View } from "react-native";
import { router } from "expo-router";

import { useAuth } from "../../src/auth/AuthContext";
import { useRecorder } from "../../src/recorder/RecorderContext";
import { BetterRouteBanner } from "../../src/components/BetterRouteBanner";
import {
  MapCanvas,
  type MapCanvasHandle,
} from "../../src/components/MapCanvas";
import { MapFabs } from "../../src/components/MapFabs";
import { RaceDetailSheet } from "../../src/components/RaceDetailSheet";
import { RaceListSheet } from "../../src/components/RaceListSheet";
import { WindBarbLayer } from "../../src/components/WindBarbLayer";
import { useRouteNotifications } from "../../src/hooks/useRouteNotifications";
import { useRouting } from "../../src/hooks/useRouting";
import { useAutoStartRecorder } from "../../src/recorder/useAutoStartRecorder";
import { useWeather } from "../../src/hooks/useWeather";
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
  const { selectedRace, setSelectedRace, recorder, startRecording } =
    useRecorder();

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
    if (!windGrid || !viewport || !windOn) {
      // eslint-disable-next-line no-console
      console.log("[barbs] empty —", {
        hasGrid: !!windGrid,
        hasViewport: !!viewport,
        windOn,
      });
      return [];
    }
    const feats = computeBarbFeatures(viewport, windGrid, null);
    // eslint-disable-next-line no-console
    console.log("[barbs] computed:", {
      count: feats.length,
      sample: feats[0]
        ? {
            lon: feats[0].geometry.coordinates[0],
            lat: feats[0].geometry.coordinates[1],
            bucket: feats[0].properties.bucket,
            dir: Math.round(feats[0].properties.dir),
          }
        : null,
      vp: { z: viewport.zoom.toFixed(2), centerLat: viewport.centerLat.toFixed(3) },
    });
    return feats;
  }, [windGrid, viewport, windOn]);

  // Routing.
  const routing = useRouting(selectedRace?.id ?? null);

  // SSE notifications — only active when a race is selected.
  const notifications = useRouteNotifications(selectedRace?.id ?? null);

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
        <WindBarbLayer features={barbFeatures} visible={windOn} />
      </MapCanvas>

      <MapFabs
        onLocateMe={() => mapRef.current?.locateMe()}
        onToggleWind={() => setWindOn((v) => !v)}
        onToggleCompass={() => mapRef.current?.toggleCompass()}
        windOn={windOn}
        headingDeg={viewport?.headingDeg ?? 0}
      />

      {selectedRace ? (
        <RaceDetailSheet
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
            />
          }
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
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
});
