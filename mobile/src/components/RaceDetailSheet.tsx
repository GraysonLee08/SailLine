// RaceDetailSheet.tsx — bottom sheet for the currently-selected race.
//
// Replaces the race list when the user taps a row. Shows the race
// metadata (date, marks, boat class), the routing controls (Compute
// route + status), and the recording CTA (Start recording).
//
// Snap points are slightly different from the list sheet: this one
// peeks higher (28%) because the most important action ("Start
// recording") lives in the peek strip — the user shouldn't have to
// pull up the sheet to find it.

import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import BottomSheet, { BottomSheetScrollView } from "@gorhom/bottom-sheet";
import { Ionicons } from "@expo/vector-icons";

import { formatRaceDate } from "../lib/formatRaceDate";
import { useTheme } from "../theme/ThemeProvider";
import { OrientationControls } from "./OrientationControls";
import type { Calibration, PhoneAxis } from "../hooks/useHeelGauge";
import type { Race } from "../types";
import type { RouteMeta } from "../api/routing";

/** Imperative API exposed to the parent so map FABs can collapse the
    sheet to peek (Minimize FAB). */
export type RaceDetailSheetHandle = {
  snapToPeek: () => void;
};

type Props = {
  race: Race;
  onClose: () => void;
  onStart: () => void;
  /** "Compute route" button handler. */
  onCompute: () => void;
  routeLoading: boolean;
  routeError: string | null;
  routeMeta: RouteMeta | null;
  /** Forecast not yet published — show countdown instead of an error. */
  routePending: {
    detail: string;
    availableAt: string;
    hoursUntilAvailable: number;
  } | null;
  autoStart: {
    armed: boolean;
    fired: boolean;
    msUntilFire: number | null;
  };
  /** Better-route SSE banner — optional, rendered above the actions. */
  betterRouteBanner?: React.ReactNode;
  /** Orientation calibration controls — persists per-race in AsyncStorage. */
  orientation?: {
    phoneAxis: PhoneAxis;
    onPhoneAxisChange: (axis: PhoneAxis) => void;
    calibration: Calibration | null;
    onCaptureCalibration: (cal: Calibration | null) => void;
    /** Whether the gauge listener should be running (sheet visible + alive). */
    enabled: boolean;
  };
  /** Auto-recompute on wind shift — countdown auto-accepts unless declined. */
  autoRoute?: {
    enabled: boolean;
    onToggle: (next: boolean) => void;
  };
  /** Called when the sheet's snap index changes. `true` means expanded
      (75% snap), `false` means peeked (32%). Used by the parent to show
      the Minimize FAB only while expanded. */
  onExpandedChange?: (expanded: boolean) => void;
};

const SNAP_POINTS = ["32%", "75%"];

export const RaceDetailSheet = forwardRef<RaceDetailSheetHandle, Props>(function RaceDetailSheet({
  race,
  onClose,
  onStart,
  onCompute,
  routeLoading,
  routeError,
  routeMeta,
  routePending,
  autoStart,
  betterRouteBanner,
  orientation,
  autoRoute,
  onExpandedChange,
}, ref) {
  const { colors, font, size, tabularVariant } = useTheme();
  const sheetRef = useRef<BottomSheet>(null);

  // Imperative API: parent FABs can snap us back to peek.
  useImperativeHandle(ref, () => ({
    snapToPeek: () => sheetRef.current?.snapToIndex(0),
  }), []);

  useEffect(() => {
    // Open to peek (showing the Start button) whenever the race changes.
    sheetRef.current?.snapToIndex(0);
    // Reset parent's expanded tracking — a new race always starts peeked.
    onExpandedChange?.(false);
  }, [race.id, onExpandedChange]);

  // If routing surfaces a pending (forecast-not-out) or hard error, the
  // explanatory text lives in the route block lower in the sheet body.
  // At the default 32% peek snap it's clipped — auto-expand to 75% so
  // the user actually sees "Forecast available in 2.3h" or the error
  // string. Re-collapses on the next race change.
  useEffect(() => {
    if (routePending || routeError) {
      sheetRef.current?.snapToIndex(1);
    }
  }, [routePending, routeError]);

  const handleIndicator = useMemo(
    () => ({ backgroundColor: colors.scrim.handle }),
    [colors.scrim.handle],
  );
  const sheetBg = useMemo(
    () => ({ backgroundColor: colors.surface.sheet }),
    [colors.surface.sheet],
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
      onChange={(index) => onExpandedChange?.(index > 0)}
    >
      <BottomSheetScrollView contentContainerStyle={styles.body}>
        {betterRouteBanner}

        <View style={styles.headerRow}>
          <View style={{ flex: 1 }}>
            <Text
              style={{
                color: colors.text.primary,
                fontFamily: font.displaySemibold,
                fontSize: size.title,
                letterSpacing: -0.3,
              }}
              numberOfLines={2}
            >
              {race.name}
            </Text>
            <Text
              style={{
                color: colors.text.secondary,
                fontFamily: font.body,
                fontSize: size.small,
                marginTop: 4,
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
            {/* ETA chip — Google-Maps-style "sailboat icon + duration"
                under the metadata. Always rendered; shows "—" until the
                user computes a route. Phase 1 spec §3 #4. */}
            <View style={styles.etaChip}>
              <Ionicons name="boat" size={14} color={colors.accent.primary} />
              <Text
                style={[
                  {
                    color: colors.text.primary,
                    fontFamily: font.tabularBold,
                    fontSize: size.small,
                  },
                  tabularVariant,
                ]}
              >
                {routeMeta ? formatMinutes(routeMeta.total_minutes) : "—"}
              </Text>
              <Text
                style={{
                  color: colors.text.muted,
                  fontFamily: font.body,
                  fontSize: size.caption,
                  marginLeft: 2,
                }}
              >
                est. race time
              </Text>
            </View>
          </View>
          <Pressable
            onPress={onClose}
            accessibilityLabel="Close race details"
            hitSlop={10}
            style={({ pressed }) => ({ opacity: pressed ? 0.6 : 1 })}
          >
            <Ionicons name="close" size={22} color={colors.text.muted} />
          </Pressable>
        </View>

        {/* Auto-start banner. Shown only when armed and not yet fired. */}
        {autoStart.armed ? (
          <View
            style={[
              styles.banner,
              {
                backgroundColor: `${colors.accent.primary}1a`,
                borderColor: `${colors.accent.primary}33`,
              },
            ]}
          >
            <Ionicons
              name="alarm-outline"
              size={16}
              color={colors.accent.primary}
            />
            <Text
              style={{
                color: colors.text.primary,
                fontFamily: font.body,
                fontSize: size.small,
                flex: 1,
              }}
            >
              Auto-start armed
              {autoStart.msUntilFire != null && autoStart.msUntilFire > 0
                ? ` — fires in ${formatCountdown(autoStart.msUntilFire)}`
                : ""}
            </Text>
          </View>
        ) : null}

        {/* PRIMARY CTA — recording. */}
        <Pressable
          onPress={onStart}
          accessibilityLabel="Start recording"
          style={({ pressed }) => [
            styles.primaryCta,
            {
              backgroundColor: pressed
                ? colors.accent.primaryPressed
                : colors.accent.primary,
            },
          ]}
        >
          <Ionicons name="radio-button-on" size={20} color={colors.text.onAccent} />
          <Text
            style={{
              color: colors.text.onAccent,
              fontFamily: font.bodySemibold,
              fontSize: size.bodyLg,
            }}
          >
            Start recording
          </Text>
        </Pressable>

        {/* Route compute. Secondary action — sits below recording. */}
        <View style={styles.routeBlock}>
          <View style={styles.routeHeader}>
            <Text
              style={{
                color: colors.text.primary,
                fontFamily: font.displaySemibold,
                fontSize: size.subtitle,
              }}
            >
              Pre-race route
            </Text>
            <Pressable
              onPress={onCompute}
              disabled={routeLoading}
              accessibilityLabel="Compute optimal route"
              style={({ pressed }) => [
                styles.secondaryButton,
                {
                  borderColor: colors.border.divider,
                  backgroundColor: pressed
                    ? colors.surface.elevated
                    : colors.surface.sheet,
                  opacity: routeLoading ? 0.7 : 1,
                },
              ]}
            >
              {routeLoading ? (
                <ActivityIndicator color={colors.accent.primary} size="small" />
              ) : (
                <Ionicons name="git-branch" size={16} color={colors.text.primary} />
              )}
              <Text
                style={{
                  color: colors.text.primary,
                  fontFamily: font.bodyMedium,
                  fontSize: size.small,
                }}
              >
                {routeLoading ? "Computing…" : routeMeta ? "Recompute" : "Compute"}
              </Text>
            </Pressable>
          </View>

          {routePending ? (
            <Text
              style={{
                color: colors.accent.warning,
                fontFamily: font.body,
                fontSize: size.small,
                lineHeight: 18,
              }}
            >
              Forecast available in{" "}
              <Text style={[{ fontFamily: font.tabularBold }, tabularVariant]}>
                {routePending.hoursUntilAvailable.toFixed(1)}h
              </Text>
              . Try again then.
            </Text>
          ) : null}

          {routeError ? (
            <Text
              style={{
                color: colors.accent.recording,
                fontFamily: font.body,
                fontSize: size.small,
              }}
            >
              {routeError}
            </Text>
          ) : null}

          {routeMeta ? (
            <View style={styles.metaRow}>
              <Metric
                label="ETA"
                value={formatMinutes(routeMeta.total_minutes)}
              />
              <Metric label="Tacks" value={String(routeMeta.tack_count)} />
              <Metric
                label="Wind"
                value={
                  routeMeta.start_wind_speed_kt != null
                    ? `${routeMeta.start_wind_speed_kt.toFixed(0)} kt`
                    : "—"
                }
              />
            </View>
          ) : null}

          {/* Auto-route toggle — when on, better-route alerts auto-accept
              after the banner's countdown unless the user declines. */}
          {autoRoute ? (
            <Pressable
              onPress={() => autoRoute.onToggle(!autoRoute.enabled)}
              accessibilityRole="switch"
              accessibilityState={{ checked: autoRoute.enabled }}
              style={({ pressed }) => [
                styles.toggleRow,
                {
                  borderColor: colors.border.divider,
                  backgroundColor: pressed
                    ? colors.surface.elevated
                    : "transparent",
                },
              ]}
            >
              <View style={{ flex: 1 }}>
                <Text
                  style={{
                    color: colors.text.primary,
                    fontFamily: font.bodySemibold,
                    fontSize: size.body,
                  }}
                >
                  Auto-route on wind shift
                </Text>
                <Text
                  style={{
                    color: colors.text.muted,
                    fontFamily: font.body,
                    fontSize: size.caption,
                    marginTop: 2,
                  }}
                >
                  Faster routes apply automatically after a 10s countdown.
                  Tap Decline to keep your current route.
                </Text>
              </View>
              <View
                style={[
                  styles.toggleTrack,
                  {
                    backgroundColor: autoRoute.enabled
                      ? colors.accent.primary
                      : colors.border.divider,
                  },
                ]}
              >
                <View
                  style={[
                    styles.toggleThumb,
                    {
                      backgroundColor: colors.surface.sheet,
                      transform: [{ translateX: autoRoute.enabled ? 18 : 2 }],
                    },
                  ]}
                />
              </View>
            </Pressable>
          ) : null}
        </View>

        {/* Orientation calibration — Fore-aft / Port-stbd / Zero. */}
        {orientation ? (
          <View style={styles.orientationBlock}>
            <Text
              style={{
                color: colors.text.primary,
                fontFamily: font.displaySemibold,
                fontSize: size.subtitle,
              }}
            >
              Orientation
            </Text>
            <OrientationControls
              enabled={orientation.enabled}
              phoneAxis={orientation.phoneAxis}
              onPhoneAxisChange={orientation.onPhoneAxisChange}
              calibration={orientation.calibration}
              onCaptureCalibration={orientation.onCaptureCalibration}
              showLiveReadout
            />
          </View>
        ) : null}
      </BottomSheetScrollView>
    </BottomSheet>
  );
});

function Metric({ label, value }: { label: string; value: string }) {
  const { colors, font, size, tabularVariant } = useTheme();
  return (
    <View style={styles.metric}>
      <Text
        style={{
          color: colors.text.muted,
          fontFamily: font.body,
          fontSize: size.caption,
          letterSpacing: 0.5,
          textTransform: "uppercase",
        }}
      >
        {label}
      </Text>
      <Text
        style={[
          {
            color: colors.text.primary,
            fontFamily: font.tabularBold,
            fontSize: size.title,
            letterSpacing: -0.5,
            marginTop: 2,
          },
          tabularVariant,
        ]}
      >
        {value}
      </Text>
    </View>
  );
}

function formatCountdown(ms: number): string {
  const totalSec = Math.round(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const totalMin = Math.round(totalSec / 60);
  if (totalMin < 60) return `${totalMin} min`;
  const hr = Math.floor(totalMin / 60);
  const min = totalMin % 60;
  return min > 0 ? `${hr}h ${min}m` : `${hr}h`;
}

function formatMinutes(mins: number): string {
  if (!Number.isFinite(mins) || mins <= 0) return "—";
  const totalSec = Math.round(mins * 60);
  const h = Math.floor(totalSec / 3600);
  const m = Math.round((totalSec % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

const styles = StyleSheet.create({
  body: {
    paddingHorizontal: 20,
    paddingTop: 4,
    paddingBottom: 36,
    gap: 16,
  },
  headerRow: {
    flexDirection: "row",
    alignItems: "flex-start",
    gap: 12,
  },
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 10,
    borderWidth: StyleSheet.hairlineWidth,
  },
  primaryCta: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 8,
    height: 52,
    borderRadius: 14,
  },
  routeBlock: { gap: 10 },
  routeHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  secondaryButton: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 12,
    height: 36,
    borderRadius: 10,
    borderWidth: StyleSheet.hairlineWidth,
  },
  metaRow: {
    flexDirection: "row",
    gap: 18,
    paddingTop: 6,
  },
  metric: { flex: 1 },
  toggleRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 12,
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 10,
    borderWidth: StyleSheet.hairlineWidth,
    marginTop: 6,
  },
  toggleTrack: {
    width: 38,
    height: 22,
    borderRadius: 999,
    justifyContent: "center",
  },
  toggleThumb: {
    width: 18,
    height: 18,
    borderRadius: 999,
  },
  orientationBlock: {
    gap: 6,
  },
  etaChip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    marginTop: 6,
  },
});
