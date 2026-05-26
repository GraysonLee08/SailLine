// RecorderScreen.tsx — record telemetry for a specific race.
//
// Extracted from the Phase 1 App.tsx test harness. Differences:
//   - takes a typed Race prop instead of a free-text raceId input
//   - shows the race name, start time, and mark count up top
//   - "Back to races" button (DISABLED while recording, to prevent the
//     user from accidentally tearing down the recorder mid-race — they
//     must stop first)
//   - shows the auto-start hook's status (armed / firing / fired)
//
// The recorder hook itself is owned by App.tsx and passed in as a prop
// so its lifetime is bound to the signed-in session, not to this screen
// mounting/unmounting. (If we ever do let the user switch races without
// stopping, that would require ref-counted lifetimes — not needed for
// the race-day MVP.)

import {
  Button,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from "react-native";

import { formatRaceDate } from "../lib/formatRaceDate";
import type { LocalPoint } from "../recorder/backgroundGeolocation";
import { useAutoStartRecorder } from "../recorder/useAutoStartRecorder";
import type { Race } from "../types";

type RecorderApi = {
  recording: boolean;
  error: string | null;
  points: ReadonlyArray<LocalPoint>;
  queueLength: number;
  lastPoint: LocalPoint | null;
  start: () => Promise<void>;
  stop: () => Promise<void>;
};

type Props = {
  race: Race;
  recorder: RecorderApi;
  /** Called when user wants to go back to the picker. UI prevents this
   *  while recording — callers don't need to enforce it themselves. */
  onBack: () => void;
  /** App-level handler that nudges the OS battery exemption first. */
  onStart: () => Promise<void>;
};

export default function RecorderScreen({
  race,
  recorder,
  onBack,
  onStart,
}: Props) {
  // Auto-start at race.start_at - 5min, but only if not already recording.
  // Enabled whenever this screen is mounted with a race that has a
  // start_at. The hook is internally idempotent against `recording`.
  const auto = useAutoStartRecorder({
    raceId: race.id,
    startAtIso: race.start_at,
    enabled: true,
    recording: recorder.recording,
    start: onStart,
  });

  // Countdown is computed off auto.msUntilFire which the hook sets once
  // at arming. It does NOT tick down on its own — the displayed value
  // is "ms-until-fire at the moment we armed." That's intentional for
  // the MVP banner; precise live countdown would require a per-second
  // timer here and isn't worth the re-render cost. The hook will
  // re-fire its effect (and reset msUntilFire) if start_at changes.
  const countdownLabel = formatCountdown(auto.msUntilFire);

  return (
    <ScrollView contentContainerStyle={styles.container}>
      <View style={styles.headerRow}>
        <Button
          title="‹ Races"
          color="#5e7d8c"
          onPress={onBack}
          disabled={recorder.recording}
        />
      </View>

      <Text style={styles.title} numberOfLines={2}>
        {race.name}
      </Text>
      <Text style={styles.subtitle}>{formatRaceDate(race.start_at)}</Text>
      <Text style={styles.subtitle}>
        {race.mode} · {race.boat_class} · {race.marks.length}{" "}
        {race.marks.length === 1 ? "mark" : "marks"}
      </Text>

      {auto.armed ? (
        <View style={styles.armedBanner}>
          <Text style={styles.armedText}>
            Auto-start armed{countdownLabel ? ` — fires in ${countdownLabel}` : ""}
          </Text>
        </View>
      ) : null}
      {auto.fired && !recorder.recording ? (
        <View style={styles.firedBanner}>
          <Text style={styles.armedText}>
            Auto-start fired but recorder is stopped. Tap Start.
          </Text>
        </View>
      ) : null}

      <View style={styles.controls}>
        {recorder.recording ? (
          <Button
            title="Stop recording"
            color="#c0392b"
            onPress={recorder.stop}
          />
        ) : (
          <Button title="Start recording" onPress={onStart} />
        )}
      </View>

      <Stat label="Status" value={recorder.recording ? "RECORDING" : "stopped"} />
      <Stat
        label="Captured (this session)"
        value={String(recorder.points.length)}
      />
      <Stat label="Unflushed queue" value={String(recorder.queueLength)} />
      <Stat
        label="Last fix"
        value={
          recorder.lastPoint
            ? `${recorder.lastPoint.lat.toFixed(5)}, ${recorder.lastPoint.lon.toFixed(5)}`
            : "—"
        }
      />
      {recorder.error ? (
        <View style={styles.row}>
          <Text style={styles.label}>Error</Text>
          <Text style={styles.error}>{recorder.error}</Text>
        </View>
      ) : null}

      <Text style={styles.note}>
        Lock the screen and the recorder keeps running (Transistorsoft
        foreground service). If "Captured" stops climbing, check that
        battery optimisation is disabled for SailLine.
      </Text>
    </ScrollView>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.row}>
      <Text style={styles.label}>{label}</Text>
      <Text style={styles.value}>{value}</Text>
    </View>
  );
}

/** "5 min", "1 hr 12 min", "30 sec", or null when ms is null/<=0. */
function formatCountdown(ms: number | null): string | null {
  if (ms === null || ms <= 0) return null;
  const totalSec = Math.round(ms / 1000);
  if (totalSec < 60) return `${totalSec} sec`;
  const totalMin = Math.round(totalSec / 60);
  if (totalMin < 60) return `${totalMin} min`;
  const hr = Math.floor(totalMin / 60);
  const min = totalMin % 60;
  return min > 0 ? `${hr} hr ${min} min` : `${hr} hr`;
}

const styles = StyleSheet.create({
  container: {
    flexGrow: 1,
    backgroundColor: "#0b1f2a",
    paddingTop: 60,
    paddingHorizontal: 24,
    gap: 12,
  },
  headerRow: {
    flexDirection: "row",
    alignItems: "center",
    marginBottom: 8,
  },
  title: { color: "#f5f7fa", fontSize: 22, fontWeight: "700" },
  subtitle: { color: "#8fb4c7", fontSize: 13 },
  controls: { marginVertical: 12 },
  row: { backgroundColor: "#13303f", borderRadius: 10, padding: 14 },
  label: { color: "#8fb4c7", fontSize: 12, marginBottom: 4 },
  value: { color: "#f5f7fa", fontSize: 16, fontWeight: "600" },
  error: { color: "#e08a8a", fontSize: 13 },
  note: { color: "#5e7d8c", fontSize: 12, marginTop: 20, lineHeight: 16 },
  armedBanner: {
    backgroundColor: "rgba(143, 180, 199, 0.15)",
    borderRadius: 10,
    padding: 12,
    marginTop: 8,
  },
  firedBanner: {
    backgroundColor: "rgba(239, 159, 39, 0.15)",
    borderRadius: 10,
    padding: 12,
    marginTop: 8,
  },
  armedText: { color: "#f5f7fa", fontSize: 13 },
});
