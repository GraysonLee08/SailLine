// LayersPanel.tsx — popover that lists every toggleable map layer.
//
// Paired with the Layers FAB inside MapFabs. Tapping the FAB sets
// ``open`` to true; the panel slides in just to the left of the FAB
// cluster. Tapping outside (the transparent backdrop) closes it.
//
// Replaces the previous per-layer FAB sprawl (Wind FAB + Actual-route
// FAB) per the 2026-06-04 user feedback: a single icon that expands
// into a list is easier to scan than a stack of opaque pictograms,
// and it scales when we add waves + map style later without crowding
// the right rail.
//
// Layer rows:
//   * Route        — calculated/pre-race route polyline (always rendered
//                    in RouteLayer; parent passes ``route={null}`` when
//                    the user toggles this off).
//   * Actual route — live recorded track on /recording. Only shown when
//                    the parent has a track to render (parent passes
//                    ``showActualRouteRow``).
//   * Wind         — wind barb overlay.
//   * Waves        — disabled today. Backend WW3 ingest isn't shipped.
//                    Row rendered with a "Soon" pill so the feature is
//                    discoverable.
//
// Each switch hits the matching key in useLayerSettings via the
// onToggleLayer callback the parent provides.

import { Modal, Pressable, StyleSheet, Switch, Text, View } from "react-native";
import { Ionicons } from "@expo/vector-icons";
import { useSafeAreaInsets } from "react-native-safe-area-context";

import { useTheme } from "../theme/ThemeProvider";
import type { LayerKey } from "../hooks/useLayerSettings";

type RowSpec = {
  key: LayerKey;
  icon: keyof typeof Ionicons.glyphMap;
  label: string;
  /** Optional secondary label for context (e.g. "Pre-race plan"). */
  detail?: string;
  /** When true, the switch is greyed out and a Soon badge renders. */
  disabled?: boolean;
};

type Props = {
  visible: boolean;
  onClose: () => void;
  /** Current on/off state of each layer. */
  layers: {
    route: boolean;
    actualRoute: boolean;
    wind: boolean;
    waves: boolean;
  };
  onToggleLayer: (key: LayerKey, value: boolean) => void;
  /** Whether the parent has an "actual route" worth rendering — the row
   *  is hidden on the home map (no recording in progress). */
  showActualRouteRow: boolean;
};

export function LayersPanel({
  visible,
  onClose,
  layers,
  onToggleLayer,
  showActualRouteRow,
}: Props) {
  const { colors, font, size } = useTheme();
  const insets = useSafeAreaInsets();

  // Match the MapFabs cluster offset so the panel anchors visually to
  // the Layers FAB. Cluster lives at top: insets.top + 16, with three
  // FABs * (48 height + 12 gap) before the Layers one in the stack.
  // The panel slides in to its LEFT and snaps to the same top.
  const topAnchor = insets.top + 16;

  const rows: RowSpec[] = [
    {
      key: "route",
      icon: "git-network-outline",
      label: "Calculated route",
      detail: "Pre-race plan",
    },
    ...(showActualRouteRow
      ? [
          {
            key: "actualRoute" as const,
            icon: "footsteps-outline" as const,
            label: "Actual route",
            detail: "Live track",
          },
        ]
      : []),
    {
      key: "wind",
      icon: "navigate-outline",
      label: "Wind",
      detail: "HRRR / GFS barbs",
    },
    {
      key: "waves",
      icon: "water-outline",
      label: "Waves",
      detail: "WaveWatch III",
      disabled: true,
    },
  ];

  return (
    <Modal
      visible={visible}
      transparent
      animationType="fade"
      onRequestClose={onClose}
    >
      {/* Backdrop catches taps anywhere off the panel to dismiss. */}
      <Pressable style={styles.backdrop} onPress={onClose}>
        {/* Inner Pressable stops the press from bubbling so taps INSIDE
            the panel don't dismiss it. */}
        <Pressable
          onPress={(e) => e.stopPropagation()}
          style={[
            styles.panel,
            {
              top: topAnchor,
              backgroundColor: colors.surface.elevated,
              borderColor: colors.border.hairline,
              shadowColor: colors.scrim.shadow,
            },
          ]}
        >
          <View style={styles.header}>
            <Text
              style={{
                color: colors.text.primary,
                fontFamily: font.bodySemibold,
                fontSize: size.body,
              }}
            >
              Layers
            </Text>
            <Pressable
              onPress={onClose}
              hitSlop={10}
              accessibilityLabel="Close layers panel"
            >
              <Ionicons name="close" size={18} color={colors.text.muted} />
            </Pressable>
          </View>

          {rows.map((row) => (
            <View
              key={row.key}
              style={[
                styles.row,
                { borderColor: colors.border.hairline },
              ]}
            >
              <View style={styles.rowIcon}>
                <Ionicons
                  name={row.icon}
                  size={20}
                  color={
                    row.disabled ? colors.text.muted : colors.text.primary
                  }
                />
              </View>
              <View style={styles.rowText}>
                <View style={styles.rowTitleLine}>
                  <Text
                    style={{
                      color: row.disabled
                        ? colors.text.muted
                        : colors.text.primary,
                      fontFamily: font.bodyMedium,
                      fontSize: size.body,
                    }}
                  >
                    {row.label}
                  </Text>
                  {row.disabled ? (
                    <View
                      style={[
                        styles.soonPill,
                        {
                          backgroundColor: `${colors.accent.warning}22`,
                          borderColor: `${colors.accent.warning}55`,
                        },
                      ]}
                    >
                      <Text
                        style={{
                          color: colors.accent.warning,
                          fontFamily: font.bodyBold,
                          fontSize: size.caption,
                          letterSpacing: 0.4,
                        }}
                      >
                        SOON
                      </Text>
                    </View>
                  ) : null}
                </View>
                {row.detail ? (
                  <Text
                    style={{
                      color: colors.text.muted,
                      fontFamily: font.body,
                      fontSize: size.small,
                    }}
                  >
                    {row.detail}
                  </Text>
                ) : null}
              </View>
              <Switch
                value={layers[row.key]}
                onValueChange={(v) => onToggleLayer(row.key, v)}
                disabled={row.disabled}
                accessibilityLabel={`Toggle ${row.label} layer`}
              />
            </View>
          ))}
        </Pressable>
      </Pressable>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: {
    flex: 1,
  },
  panel: {
    position: "absolute",
    right: 76, // sit immediately left of the 48-wide FAB column + 12 gap + 16 rail margin
    width: 260,
    borderRadius: 16,
    borderWidth: StyleSheet.hairlineWidth,
    paddingVertical: 6,
    paddingHorizontal: 14,
    shadowOffset: { width: 0, height: 6 },
    shadowOpacity: 1,
    shadowRadius: 14,
    elevation: 6,
  },
  header: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingTop: 10,
    paddingBottom: 6,
  },
  row: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    paddingVertical: 10,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  rowIcon: {
    width: 24,
    alignItems: "center",
  },
  rowText: {
    flex: 1,
  },
  rowTitleLine: {
    flexDirection: "row",
    alignItems: "center",
    gap: 6,
  },
  soonPill: {
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: 8,
    borderWidth: StyleSheet.hairlineWidth,
  },
});
