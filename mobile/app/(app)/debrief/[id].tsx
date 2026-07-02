// app/(app)/debrief/[id].tsx — post-race MAP DEBRIEF.
//
// The screen the recorder lands on the moment recording stops, and the
// one finished races open from the home list — it supersedes the
// text-only Review screen (race-review/[id].tsx stays routable as a
// fallback). Layout:
//   * Top ~55%: read-only map — course marks, the computed (planned)
//     route in blue, the recorded track in red (ActualRouteLayer),
//     camera fitted to course + track.
//   * Bottom ~45%: scrollable panel with the same leg stats + AI recap
//     the Review screen shows (shared RaceReviewSections).
//   * Done chip (top-left over the map) → home.
//
// Track source resolution (memory first, backend fallback):
//   1. recorder.points — when the recorder's selectedRace matches this
//      debrief and points are non-empty (the just-stopped case). Memory
//      is authoritative and instant; the backend copy may still be
//      mid-flush. Points survive stop() by design — the recorder resets
//      them on the NEXT start (see useTrackRecorder.start).
//   2. GET /api/races/{id}/track — persisted points, for old races
//      opened from the list or after an app restart.
//
// Computed-route resolution mirrors the track:
//   1. RoutingContext — populated when this race is still the selected
//      race (the plan survives the Stop navigation from /recording).
//   2. Direct POST /api/routing/compute — idempotent + Redis-cached, so
//      it returns the same plan the sailor raced against. A 425 or any
//      error silently omits the layer: the debrief is read-only, there
//      is no Compute button to retry from here by design.
//
// Deliberately NOT here (follow-ups): time scrubber, tactician-call pins.

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { router, useLocalSearchParams } from "expo-router";
import { Ionicons } from "@expo/vector-icons";

import { ActualRouteLayer } from "../../../src/components/ActualRouteLayer";
import {
  MapCanvas,
  type MapCanvasHandle,
} from "../../../src/components/MapCanvas";
import { RaceReviewSections } from "../../../src/components/RaceReviewSections";
import { formatRaceDate } from "../../../src/lib/formatRaceDate";
import { useRaceStats } from "../../../src/hooks/useRaceStats";
import { useRecorder } from "../../../src/recorder/RecorderContext";
import { useRoute } from "../../../src/routing/RoutingContext";
import { useTheme } from "../../../src/theme/ThemeProvider";
import { getTrack } from "../../../src/api/races";
import { computeRoute, type RouteFeature } from "../../../src/api/routing";
import type { LocalPoint } from "../../../src/recorder/backgroundGeolocation";
import type { RaceMark } from "../../../src/types";

export default function DebriefScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const raceId = id ?? null;
  const { colors, font, size } = useTheme();
  const { selectedRace, recorder } = useRecorder();
  const routing = useRoute();
  const { data, phase, error, refresh } = useRaceStats(raceId);

  // Whether the app-wide recorder/routing state is still pointed at THIS
  // race — true in the just-stopped case, false when an old race was
  // opened from the home list.
  const isCurrentRace = raceId != null && selectedRace?.id === raceId;

  // ── Track: memory first, backend fallback ──────────────────────────
  const memoryTrack =
    isCurrentRace && recorder.points.length > 0 ? recorder.points : null;
  const [fetchedTrack, setFetchedTrack] = useState<LocalPoint[] | null>(null);
  useEffect(() => {
    if (memoryTrack || !raceId) return;
    let cancelled = false;
    getTrack(raceId)
      .then((pts) => {
        if (cancelled) return;
        // TrackPointOut lacks gps_acc_m; ActualRouteLayer only reads
        // lat/lon, so a null fill keeps the LocalPoint shape honest.
        setFetchedTrack(pts.map((p) => ({ ...p, gps_acc_m: null })));
      })
      .catch(() => {
        /* no persisted track — the map still shows marks + route */
      });
    return () => {
      cancelled = true;
    };
  }, [memoryTrack, raceId]);
  const trackPoints: readonly LocalPoint[] =
    memoryTrack ?? fetchedTrack ?? [];

  // ── Computed route: shared context first, direct compute fallback ──
  const contextRoute = isCurrentRace ? routing.route : null;
  const [localRoute, setLocalRoute] = useState<RouteFeature | null>(null);
  useEffect(() => {
    if (!raceId || contextRoute || localRoute) return;
    let cancelled = false;
    computeRoute(raceId)
      .then((result) => {
        if (!cancelled && result.kind === "ok") setLocalRoute(result.route);
      })
      .catch(() => {
        /* 425 / compute error — silently omit the planned-route layer */
      });
    return () => {
      cancelled = true;
    };
  }, [raceId, contextRoute, localRoute]);
  const route = contextRoute ?? localRoute;

  // Course marks: the selected race when it matches, otherwise the stats
  // payload (it carries the course for list-opened races). The stats
  // poll (20 s cadence while the AI summary generates) returns a fresh
  // marks array each time — key on content so the map doesn't refit the
  // camera on every poll (MapCanvas refits whenever `marks` identity
  // changes).
  const statsMarksKey = JSON.stringify(data?.marks ?? []);
  const marks: RaceMark[] = useMemo(() => {
    if (isCurrentRace && selectedRace) return selectedRace.marks;
    const parsed = JSON.parse(statsMarksKey) as Array<{
      lat: number;
      lon: number;
      name?: string;
    }>;
    // RaceMark requires a name; it's optional on the stats wire shape.
    return parsed.map((m) => ({ name: m.name ?? "", lat: m.lat, lon: m.lon }));
  }, [isCurrentRace, selectedRace, statsMarksKey]);

  // Fit-to-course+track. Same imperative-handle + setTimeout(0) pattern
  // as recording.tsx (the Camera ref isn't attached until after the
  // first render commit). Marks and track can arrive in either order
  // (parallel fetches on the list-opened path), so the fit re-runs when
  // either shows up — keyed on the point counts so it never re-fires on
  // identity-only changes and never yanks the camera after both have
  // landed.
  const mapRef = useRef<MapCanvasHandle>(null);
  const lastFitKeyRef = useRef<string | null>(null);
  useEffect(() => {
    const pts = [
      ...marks.map((m) => ({ lat: m.lat, lon: m.lon })),
      ...trackPoints.map((p) => ({ lat: p.lat, lon: p.lon })),
    ];
    if (pts.length === 0) return;
    const key = `${marks.length}:${trackPoints.length}`;
    if (lastFitKeyRef.current === key) return;
    lastFitKeyRef.current = key;
    const t = setTimeout(() => {
      mapRef.current?.fitToPoints(pts);
    }, 0);
    return () => clearTimeout(t);
  }, [marks, trackPoints]);

  const raceName =
    data?.name ??
    (isCurrentRace ? selectedRace?.name : null) ??
    "Race debrief";
  const subtitle = [
    data?.start_at ? formatRaceDate(data.start_at) : null,
    data?.boat_class,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <View style={[styles.root, { backgroundColor: colors.surface.page }]}>
      {/* ── Top ~55%: the debrief map ─────────────────────────────── */}
      <View style={styles.mapWrap}>
        <MapCanvas ref={mapRef} marks={marks} route={route} showUser={false}>
          {/* Recorded track (red) drawn over the planned route (blue) —
              actual is truth, planned is reference. Same convention as
              the recording screen. */}
          <ActualRouteLayer
            points={trackPoints}
            visible={trackPoints.length > 0}
          />
        </MapCanvas>

        {/* Done chip — the debrief is a terminal screen; Done replaces
            to home rather than popping back into /recording. */}
        <SafeAreaView
          edges={["top"]}
          style={styles.topBar}
          pointerEvents="box-none"
        >
          <Pressable
            onPress={() => router.replace("/")}
            accessibilityLabel="Done"
            style={({ pressed }) => [
              styles.doneChip,
              {
                backgroundColor: colors.surface.floating,
                borderColor: colors.border.hairline,
                opacity: pressed ? 0.85 : 1,
                shadowColor: colors.scrim.shadow,
              },
            ]}
          >
            <Ionicons
              name="checkmark"
              size={18}
              color={colors.text.primary}
            />
            <Text
              style={{
                color: colors.text.primary,
                fontFamily: font.bodyMedium,
                fontSize: size.small,
              }}
            >
              Done
            </Text>
          </Pressable>
        </SafeAreaView>
      </View>

      {/* ── Bottom panel: header + shared review sections ─────────── */}
      <View style={styles.panel}>
        <View style={styles.panelHeader}>
          <Text
            style={{
              color: colors.text.primary,
              fontFamily: font.displaySemibold,
              fontSize: size.title,
            }}
            numberOfLines={1}
          >
            {raceName}
          </Text>
          {subtitle ? (
            <Text
              style={{
                color: colors.text.muted,
                fontFamily: font.body,
                fontSize: size.small,
                marginTop: 1,
              }}
              numberOfLines={1}
            >
              {subtitle}
            </Text>
          ) : null}
        </View>
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
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  // ~55/45 split — map on top, stats below. Flex ratios, not fixed
  // heights, so the split holds across screen sizes.
  mapWrap: { flex: 55 },
  panel: { flex: 45 },
  panelHeader: { paddingHorizontal: 16, paddingTop: 12, paddingBottom: 2 },
  scroll: { paddingHorizontal: 16, paddingBottom: 8 },
  topBar: {
    position: "absolute",
    top: 8,
    left: 12,
  },
  doneChip: {
    flexDirection: "row",
    alignItems: "center",
    alignSelf: "flex-start",
    gap: 6,
    paddingLeft: 10,
    paddingRight: 14,
    height: 38,
    borderRadius: 19,
    borderWidth: StyleSheet.hairlineWidth,
    shadowOffset: { width: 0, height: 3 },
    shadowOpacity: 1,
    shadowRadius: 8,
    elevation: 3,
  },
});
