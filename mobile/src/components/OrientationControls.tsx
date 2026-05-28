// OrientationControls.tsx — Fore-aft / Port-stbd / Zero pill row.
//
// Mirrors the webapp's row of orientation controls inside the race
// overlay. Three pill buttons:
//   * Fore-aft — phone long edge along boat centerline (default)
//   * Port-stbd — phone long edge across the boat
//   * Zero — captures current orientation as the zero heel/pitch
//
// Plus a status line:
//   * "✓ Zeroed (heel X°, pitch Y°)" when a calibration has been captured
//   * "Heel/pitch unavailable on this device" when expo-sensors isn't
//     installed yet or the IMU has no fix
//
// And, while recording, a live heel readout subtly tucked at the right.

import { Pressable, StyleSheet, Text, View } from "react-native";

import { useTheme } from "../theme/ThemeProvider";
import { captureCalibration, useHeelGauge } from "../hooks/useHeelGauge";
import type {
  Calibration,
  HeelReading,
  PhoneAxis,
} from "../hooks/useHeelGauge";

type Props = {
  enabled: boolean;
  phoneAxis: PhoneAxis;
  onPhoneAxisChange: (axis: PhoneAxis) => void;
  calibration: Calibration | null;
  onCaptureCalibration: (cal: Calibration | null) => void;
  /** When true, show the live heel/pitch readout next to the controls. */
  showLiveReadout?: boolean;
};

export function OrientationControls({
  enabled,
  phoneAxis,
  onPhoneAxisChange,
  calibration,
  onCaptureCalibration,
  showLiveReadout = false,
}: Props) {
  const { colors, font, size } = useTheme();

  // Drive the gauge so the live readout (and the Zero button's snapshot)
  // both work. Gating on `enabled` lets the caller pause the listener
  // when the sheet isn't visible.
  const { reading, supported } = useHeelGauge({
    enabled,
    phoneAxis,
    calibration,
  });

  const handleZero = () => {
    const captured = captureCalibration(phoneAxis);
    if (captured) {
      onCaptureCalibration(captured);
    }
  };

  const handleClear = () => onCaptureCalibration(null);

  const labelStyle = {
    color: colors.text.muted,
    fontFamily: font.body,
    fontSize: size.caption,
  };

  return (
    <View style={styles.root}>
      <View style={styles.row}>
        <Text style={[labelStyle, styles.rowLabel]}>Phone</Text>
        <Pill
          label="Fore-aft"
          active={phoneAxis === "fore-aft"}
          onPress={() => onPhoneAxisChange("fore-aft")}
        />
        <Pill
          label="Port-stbd"
          active={phoneAxis === "port-stbd"}
          onPress={() => onPhoneAxisChange("port-stbd")}
        />
        <Pill
          label="Zero"
          active={!!calibration}
          onPress={handleZero}
          accent
          disabled={!supported}
        />
        {calibration && (
          <Pressable
            onPress={handleClear}
            hitSlop={6}
            accessibilityLabel="Clear calibration"
            style={({ pressed }) => [
              styles.clearBtn,
              { opacity: pressed ? 0.5 : 1 },
            ]}
          >
            <Text
              style={{
                color: colors.text.muted,
                fontFamily: font.body,
                fontSize: size.caption,
              }}
            >
              clear
            </Text>
          </Pressable>
        )}
        {showLiveReadout && <LiveReadout reading={reading} />}
      </View>
      <StatusLine
        supported={supported}
        calibration={calibration}
      />
    </View>
  );
}

function Pill({
  label,
  active,
  onPress,
  accent = false,
  disabled = false,
}: {
  label: string;
  active: boolean;
  onPress: () => void;
  accent?: boolean;
  disabled?: boolean;
}) {
  const { colors, font, size } = useTheme();
  // Active styling: accent uses the route blue (matches the webapp's
  // "Zero" tint); non-accent uses a softer border highlight.
  const baseBg = active
    ? accent
      ? `${colors.accent.route}22`
      : `${colors.accent.route}1a`
    : "transparent";
  const border = active
    ? `${colors.accent.route}88`
    : colors.border.divider;
  const fg = active ? colors.text.primary : colors.text.secondary;

  return (
    <Pressable
      onPress={onPress}
      disabled={disabled}
      accessibilityRole="button"
      accessibilityState={{ selected: active, disabled }}
      style={({ pressed }) => [
        styles.pill,
        {
          backgroundColor: baseBg,
          borderColor: border,
          opacity: disabled ? 0.4 : pressed ? 0.7 : 1,
        },
      ]}
    >
      <Text
        style={{
          color: fg,
          fontFamily: font.bodySemibold,
          fontSize: size.caption,
        }}
      >
        {label}
      </Text>
    </Pressable>
  );
}

function LiveReadout({ reading }: { reading: HeelReading | null }) {
  const { colors, font, size, tabularVariant } = useTheme();
  if (!reading) return null;
  return (
    <View style={styles.liveReadout}>
      <Text
        style={[
          {
            color: colors.text.muted,
            fontFamily: font.body,
            fontSize: size.caption,
          },
          styles.liveLabel,
        ]}
      >
        Heel
      </Text>
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
        {reading.heelDeg.toFixed(0)}°
      </Text>
    </View>
  );
}

function StatusLine({
  supported,
  calibration,
}: {
  supported: boolean;
  calibration: Calibration | null;
}) {
  const { colors, font, size } = useTheme();
  if (!supported) {
    return (
      <Text
        style={{
          color: colors.text.muted,
          fontFamily: font.body,
          fontSize: size.caption,
          marginTop: 6,
        }}
      >
        Heel/pitch unavailable on this device.
      </Text>
    );
  }
  if (calibration) {
    return (
      <Text
        style={{
          color: colors.text.muted,
          fontFamily: font.body,
          fontSize: size.caption,
          marginTop: 6,
        }}
      >
        ✓ Zeroed (heel {calibration.heel_zero_offset_deg.toFixed(1)}°, pitch{" "}
        {calibration.pitch_zero_offset_deg.toFixed(1)}°)
      </Text>
    );
  }
  return (
    <Text
      style={{
        color: colors.text.muted,
        fontFamily: font.body,
        fontSize: size.caption,
        marginTop: 6,
      }}
    >
      Tap Zero at the dock with the boat level.
    </Text>
  );
}

const styles = StyleSheet.create({
  root: { marginTop: 8 },
  row: {
    flexDirection: "row",
    alignItems: "center",
    flexWrap: "wrap",
    gap: 6,
  },
  rowLabel: {
    textTransform: "uppercase",
    letterSpacing: 0.6,
    marginRight: 2,
  },
  pill: {
    paddingHorizontal: 10,
    paddingVertical: 5,
    borderRadius: 999,
    borderWidth: StyleSheet.hairlineWidth,
  },
  clearBtn: {
    paddingHorizontal: 4,
    paddingVertical: 2,
  },
  liveReadout: {
    marginLeft: "auto",
    flexDirection: "row",
    alignItems: "baseline",
    gap: 6,
  },
  liveLabel: {
    textTransform: "uppercase",
    letterSpacing: 0.6,
  },
});
