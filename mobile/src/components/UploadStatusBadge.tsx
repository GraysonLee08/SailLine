// UploadStatusBadge.tsx — small pill rendered next to the LIVE chip
// on the recording screen.
//
// Phase 3 of the durable upload pipeline rework. This is the first
// HONEST connectivity indicator the recorder has had. The existing
// "ON LINE" label on the GuidanceCard is cross-track distance to the
// next mark — useful for navigation but unrelated to whether data is
// reaching the backend. The 2026-05-31 race recorded 2,090 GPS points
// and showed "ON LINE" the whole time while uploads were silently
// stalled for 35+ minutes.
//
// Status colors map roughly to traffic-light semantics:
//   live      green — uploading fine
//   buffering yellow — backlog growing but healthy enough
//   stalled   orange — uploads dead-stopped, intervention worthwhile
//   offline   grey  — no network reported by the OS
//
// The component is intentionally tiny — text only ("LIVE" / "BUFFER" /
// "STALL" / "OFFLINE") plus a coloured dot, with the queue depth
// appended when it matters. Goal is at-a-glance scan, not detail.

import { StyleSheet, Text, View } from "react-native";

import { useTheme } from "../theme/ThemeProvider";

import type { UploadStatus } from "../recorder/uploadStatus";

type Props = {
  status: UploadStatus;
  queueDepth: number;
};

type StatusVisual = {
  /** Background fill — applied at ~26 alpha for the chip body. */
  baseColor: string;
  /** Color for the dot + text — full opacity for legibility. */
  accentColor: string;
  /** Short uppercase label shown on the chip. */
  label: string;
  /** Whether to append the queue depth ("STALL 47"). */
  showCount: boolean;
};

function useStatusVisual(status: UploadStatus): StatusVisual {
  const { colors } = useTheme();
  switch (status) {
    case "live":
      // Reuse the recording accent (the LIVE pill already uses this
      // colour, but at a different label — pairing them visually here
      // is intentional: both chips are saying "we're good").
      return {
        baseColor: colors.accent.recording,
        accentColor: colors.accent.recording,
        label: "LIVE",
        showCount: false,
      };
    case "buffering":
      // No dedicated warning token in the palette — pull from accent
      // primary at a different saturation. If a yellow gets added to
      // the theme later, swap it in here without touching the rest
      // of the file.
      return {
        baseColor: colors.accent.primary,
        accentColor: colors.accent.primary,
        label: "BUFFER",
        showCount: true,
      };
    case "stalled":
      // accent.stop is already an attention-grabbing colour used for
      // the big Stop button; reusing it here is fine — the badge is
      // small and the user only sees both at once if uploads die mid-
      // race, which IS the moment they need to notice.
      return {
        baseColor: colors.accent.stop,
        accentColor: colors.accent.stop,
        label: "STALL",
        showCount: true,
      };
    case "offline":
      return {
        baseColor: colors.text.muted,
        accentColor: colors.text.muted,
        label: "OFFLINE",
        showCount: true,
      };
  }
}

export function UploadStatusBadge({ status, queueDepth }: Props) {
  const { colors, font, size } = useTheme();
  const visual = useStatusVisual(status);

  return (
    <View
      style={[
        styles.chip,
        {
          backgroundColor: `${visual.baseColor}22`,
          borderColor: `${visual.baseColor}55`,
        },
      ]}
    >
      <View
        style={[styles.dot, { backgroundColor: visual.accentColor }]}
      />
      <Text
        style={{
          color: visual.accentColor,
          fontFamily: font.bodyBold,
          fontSize: size.caption,
          letterSpacing: 0.6,
        }}
      >
        {visual.showCount && queueDepth > 0
          ? `${visual.label} ${queueDepth}`
          : visual.label}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  // Sized to match the LIVE chip beside it on /recording so the two
  // line up cleanly without extra wrapper margin.
  chip: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
    paddingHorizontal: 10,
    height: 26,
    borderRadius: 13,
    borderWidth: StyleSheet.hairlineWidth,
  },
  dot: {
    width: 6,
    height: 6,
    borderRadius: 3,
  },
});
