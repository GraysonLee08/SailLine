// RaceEditScreen.tsx — full-screen mobile race editor (parity with webapp
// RaceEditor.jsx).
//
// Three ways to add marks (matches the webapp):
//   1. Pick a MORF course preset → fills start/marks/finish.
//   2. Tap on the map → drops an unnamed mark at that point.
//   3. Add an empty mark and type its lat/lon into the inputs.
//
// Empty marks have lat=null, lon=null. They're invisible on the map (no
// dot, no course-line contribution) until coords are filled in. Save
// validates that every mark has real coordinates before posting — the
// API requires float lat/lon and would 422 on null otherwise.
//
// Lat/lon input format (deg-min vs decimal) is user-toggleable and
// persisted to AsyncStorage. Storage and API are always decimal degrees.
//
// Race start time is captured as separate date + time TextInputs because
// react-native doesn't ship a native date picker; users type
// "YYYY-MM-DD" and "HH:mm". They combine into a single ISO UTC
// timestamp on save. Empty inputs serialize as null — users can save a
// course before scheduling is finalized.
//
// Tech-debt — adding @react-native-community/datetimepicker would be a
// nice usability win next session (free Android/iOS native picker).
// Held off tonight to ship without a new native dep before tomorrow's
// on-water test.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  FlatList,
  KeyboardAvoidingView,
  Modal,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import AsyncStorage from "@react-native-async-storage/async-storage";
import BottomSheet, { BottomSheetView } from "@gorhom/bottom-sheet";
import { Ionicons } from "@expo/vector-icons";
import { router } from "expo-router";

import {
  BOAT_CLASSES,
  COURSE_FAMILIES,
  buildCourseMarks,
  formatLatInput,
  formatLonInput,
  formatDecimal,
  parseCoord,
} from "@sailline/shared";

import {
  createRace,
  getRace,
  updateRace,
  type RacePayload,
} from "../api/races";
import { listBoats, type BoatOption } from "../api/boats";
import { MapCanvas, type MapCanvasHandle } from "../components/MapCanvas";
import { useTheme } from "../theme/ThemeProvider";
import type { RaceMark } from "../types";

// Persistence key for the coord format pref (parity with webapp's
// "sailline.coordFormat" localStorage key — namespace matches).
const COORD_FORMAT_KEY = "sailline.coordFormat";

type CoordFormat = "dm" | "decimal";

// Editor's working mark — superset of RaceMark with a transient `lat:null`
// state for unplaced rows the user just added but hasn't filled in.
type EditMark = {
  name: string;
  lat: number | null;
  lon: number | null;
  description?: string;
};

const isPlaced = (m: EditMark): m is EditMark & { lat: number; lon: number } =>
  Number.isFinite(m.lat) && Number.isFinite(m.lon);

const defaultMarkName = (index: number): string =>
  index === 0 ? "Start" : `Mark ${index}`;

// ── Date/time helpers — mirror webapp RaceEditor.jsx isoToLocalParts /
// localPartsToIso. Local-wall-clock strings round-trip with ISO UTC.

function isoToLocalParts(iso: string | null): { date: string; time: string } {
  if (!iso) return { date: "", time: "" };
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return { date: "", time: "" };
  const pad = (n: number) => String(n).padStart(2, "0");
  return {
    date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
    time: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
  };
}

function localPartsToIso(date: string, time: string): string | null {
  if (!date || !time) return null;
  const d = new Date(`${date}T${time}`);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString();
}

type Props = {
  /** Race id to edit. Omit for a new race. */
  raceId?: string;
};

export function RaceEditScreen({ raceId }: Props) {
  const { colors, font, size } = useTheme();
  const isNew = !raceId;

  // ── Form state (mirror of webapp RaceEditor.jsx state shape) ──────
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"inshore" | "distance">("inshore");
  const [boatClass, setBoatClass] = useState<string>(BOAT_CLASSES[0]);
  const [marks, setMarks] = useState<EditMark[]>([]);
  const [boatId, setBoatId] = useState<string | null>(null);
  const [usesSpinnaker, setUsesSpinnaker] = useState(true);
  const [autoStartEnabled, setAutoStartEnabled] = useState(true);
  const [boatOptions, setBoatOptions] = useState<BoatOption[]>([]);

  // Start time split into date + time TextInputs (combined on save).
  const [startDate, setStartDate] = useState("");
  const [startTime, setStartTime] = useState("");

  // Coord format pref — loaded from AsyncStorage on mount, persisted on
  // change. "dm" matches the sailor default in the webapp.
  const [coordFormat, setCoordFormat] = useState<CoordFormat>("dm");

  const [loading, setLoading] = useState(!isNew);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mapRef = useRef<MapCanvasHandle>(null);

  // ── Load coord format pref ────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    AsyncStorage.getItem(COORD_FORMAT_KEY)
      .then((v) => {
        if (cancelled) return;
        if (v === "decimal" || v === "dm") setCoordFormat(v);
      })
      .catch(() => {
        /* AsyncStorage unavailable — keep the dm default */
      });
    return () => {
      cancelled = true;
    };
  }, []);
  useEffect(() => {
    AsyncStorage.setItem(COORD_FORMAT_KEY, coordFormat).catch(() => {
      /* ignore */
    });
  }, [coordFormat]);

  // ── Load existing race ────────────────────────────────────────────
  useEffect(() => {
    if (isNew || !raceId) return;
    let cancelled = false;
    getRace(raceId)
      .then((race) => {
        if (cancelled) return;
        setName(race.name);
        setMode(race.mode === "distance" ? "distance" : "inshore");
        setBoatClass(race.boat_class);
        setMarks(
          (race.marks ?? []).map((m) => ({
            name: m.name,
            lat: m.lat,
            lon: m.lon,
          })),
        );
        setUsesSpinnaker(race.uses_spinnaker !== false);
        // Race row in the wire format may carry boat_id (column exists
        // server-side; mobile types.ts hasn't surfaced it yet — coerce
        // defensively rather than tighten the type and force a wider
        // refactor in this session).
        const boatIdField = (race as unknown as { boat_id?: string | null })
          .boat_id;
        setBoatId(boatIdField ?? null);
        // auto_start_enabled defaults to true if the server didn't return
        // it (older race rows pre-migration 0007 would lack the column).
        const autoStartField = (race as unknown as {
          auto_start_enabled?: boolean;
        }).auto_start_enabled;
        setAutoStartEnabled(autoStartField !== false);
        const parts = isoToLocalParts(race.start_at);
        setStartDate(parts.date);
        setStartTime(parts.time);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : String(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [raceId, isNew]);

  // ── Load boats (independent of race load) ────────────────────────
  useEffect(() => {
    let cancelled = false;
    listBoats().then((boats) => {
      if (cancelled) return;
      setBoatOptions(boats);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  // ── Mark mutations ────────────────────────────────────────────────
  const moveUp = useCallback((i: number) => {
    setMarks((prev) => {
      if (i === 0) return prev;
      const next = [...prev];
      [next[i - 1], next[i]] = [next[i], next[i - 1]];
      return next;
    });
  }, []);

  const moveDown = useCallback((i: number) => {
    setMarks((prev) => {
      if (i === prev.length - 1) return prev;
      const next = [...prev];
      [next[i], next[i + 1]] = [next[i + 1], next[i]];
      return next;
    });
  }, []);

  const deleteMark = useCallback((i: number) => {
    setMarks((prev) => prev.filter((_, idx) => idx !== i));
  }, []);

  const renameMark = useCallback((i: number, value: string) => {
    setMarks((prev) =>
      prev.map((m, idx) => (idx === i ? { ...m, name: value } : m)),
    );
  }, []);

  const updateLat = useCallback((i: number, value: number | null) => {
    setMarks((prev) =>
      prev.map((m, idx) =>
        idx === i ? { ...m, lat: value, description: undefined } : m,
      ),
    );
  }, []);

  const updateLon = useCallback((i: number, value: number | null) => {
    setMarks((prev) =>
      prev.map((m, idx) =>
        idx === i ? { ...m, lon: value, description: undefined } : m,
      ),
    );
  }, []);

  const addEmptyMark = useCallback(() => {
    setMarks((prev) => [
      ...prev,
      { name: defaultMarkName(prev.length), lat: null, lon: null },
    ]);
  }, []);

  // Tap on map → drop a placed mark with default name.
  const handleMapPress = useCallback((lat: number, lon: number) => {
    setMarks((prev) => [
      ...prev,
      { name: defaultMarkName(prev.length), lat, lon },
    ]);
  }, []);

  // ── Course preset ─────────────────────────────────────────────────
  const applyCoursePreset = useCallback(
    (courseId: string) => {
      if (!courseId) return;
      const next = buildCourseMarks(courseId) as
        | { name: string; lat: number; lon: number; description?: string }[]
        | null;
      if (!next) {
        setError(`Unknown course template: ${courseId}`);
        return;
      }
      const replace = () =>
        setMarks(
          next.map((m) => ({
            name: m.name,
            lat: m.lat,
            lon: m.lon,
            description: m.description,
          })),
        );
      if (marks.length === 0) {
        replace();
        return;
      }
      Alert.alert(
        "Replace existing marks?",
        `Loading ${courseId} will replace your current ${marks.length} mark${
          marks.length === 1 ? "" : "s"
        }.`,
        [
          { text: "Cancel", style: "cancel" },
          {
            text: "Replace",
            style: "destructive",
            onPress: replace,
          },
        ],
      );
    },
    [marks.length],
  );

  // ── Save ──────────────────────────────────────────────────────────
  const handleSave = useCallback(async () => {
    if (!name.trim()) {
      setError("Give the race a name before saving.");
      return;
    }
    const unplaced = marks.find((m) => !isPlaced(m));
    if (unplaced) {
      setError(
        `Mark "${unplaced.name || "(unnamed)"}" is missing coordinates. ` +
          `Type a lat/lon, tap the map, or remove the mark before saving.`,
      );
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const startAtIso = localPartsToIso(startDate, startTime);
      const cleanMarks: RaceMark[] = marks
        .filter(isPlaced)
        .map((m) => ({
          name: m.name,
          lat: m.lat,
          lon: m.lon,
          ...(m.description ? { description: m.description } : {}),
        })) as RaceMark[];
      const payload: RacePayload = {
        name: name.trim(),
        mode,
        boat_class: boatClass,
        marks: cleanMarks,
        start_at: startAtIso,
        auto_start_enabled: autoStartEnabled,
        boat_id: boatId,
        uses_spinnaker: usesSpinnaker,
      };
      if (isNew) {
        await createRace(payload);
      } else if (raceId) {
        await updateRace(raceId, payload);
      }
      // Pop back to the map home. The race list reloads on focus
      // (RaceListSheet refresh handler).
      router.back();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSaving(false);
    }
  }, [
    name,
    marks,
    mode,
    boatClass,
    startDate,
    startTime,
    autoStartEnabled,
    boatId,
    usesSpinnaker,
    isNew,
    raceId,
  ]);

  // ── Map render props ──────────────────────────────────────────────
  // The map renders only PLACED marks (parity with the webapp behaviour
  // — unplaced marks are invisible until coords are entered).
  const mapMarks = useMemo<RaceMark[]>(
    () => marks.filter(isPlaced).map((m) => ({ name: m.name, lat: m.lat, lon: m.lon })),
    [marks],
  );

  const handleIndicatorStyle = useMemo(
    () => ({ backgroundColor: colors.scrim.handle }),
    [colors.scrim.handle],
  );
  const sheetBg = useMemo(
    () => ({ backgroundColor: colors.surface.sheet }),
    [colors.surface.sheet],
  );

  return (
    <View style={[styles.root, { backgroundColor: colors.surface.page }]}>
      {/* Top bar — Cancel / title / Save. SafeAreaView from
          react-native-safe-area-context (not base react-native — that's
          iOS-only) so the row clears the iOS notch / Android status
          bar. edges=["top"] only — bottom inset is handled by the
          scroll content's paddingBottom + the keyboard avoider.
          2026-06-04 user report: Cancel + Save were hiding behind the
          iOS clock + battery icons in PWA-like full-screen mode. */}
      <SafeAreaView
        edges={["top"]}
        style={[
          styles.topBar,
          {
            backgroundColor: colors.surface.sheet,
            borderBottomColor: colors.border.hairline,
          },
        ]}
      >
        <Pressable
          onPress={() => router.back()}
          hitSlop={10}
          accessibilityRole="button"
          accessibilityLabel="Cancel"
        >
          <Text
            style={{
              color: colors.text.muted,
              fontFamily: font.body,
              fontSize: size.body,
            }}
          >
            Cancel
          </Text>
        </Pressable>
        <Text
          style={{
            color: colors.text.primary,
            fontFamily: font.displaySemibold,
            fontSize: size.subtitle,
          }}
        >
          {isNew ? "New race" : "Edit race"}
        </Text>
        <Pressable
          onPress={handleSave}
          disabled={saving || loading}
          hitSlop={10}
          accessibilityRole="button"
          accessibilityLabel="Save race"
          style={({ pressed }) => ({
            opacity: saving || loading ? 0.5 : pressed ? 0.7 : 1,
          })}
        >
          <Text
            style={{
              color: colors.accent.primary,
              fontFamily: font.bodySemibold,
              fontSize: size.body,
            }}
          >
            {saving ? "Saving…" : "Save"}
          </Text>
        </Pressable>
      </SafeAreaView>

      {/* Map — fills the area above the sheet. Tap-to-add-mark active. */}
      <View style={styles.mapWrap}>
        <MapCanvas
          ref={mapRef}
          marks={mapMarks}
          route={null}
          showUser
          onMapPress={handleMapPress}
        />
      </View>

      {/* Form sheet. Peek shows name + course count; full shows everything. */}
      <BottomSheet
        index={1}
        snapPoints={["18%", "55%", "92%"]}
        handleIndicatorStyle={handleIndicatorStyle}
        backgroundStyle={sheetBg}
        enableDynamicSizing={false}
        enableOverDrag={false}
      >
        <BottomSheetView style={{ flex: 1 }}>
          <KeyboardAvoidingView
            style={{ flex: 1 }}
            behavior={Platform.OS === "ios" ? "padding" : undefined}
            // Header bar height (~52) so the input stays above the keyboard.
            keyboardVerticalOffset={Platform.OS === "ios" ? 60 : 0}
          >
            <ScrollView
              contentContainerStyle={styles.scrollContent}
              keyboardShouldPersistTaps="handled"
            >
              {error ? (
                <View
                  style={[
                    styles.errorBanner,
                    { backgroundColor: `${colors.accent.recording}22` },
                  ]}
                >
                  <Text
                    style={{
                      color: colors.accent.recording,
                      fontFamily: font.body,
                      fontSize: size.small,
                    }}
                  >
                    {error}
                  </Text>
                </View>
              ) : null}

              <Section label="Name">
                <TextInput
                  value={name}
                  onChangeText={setName}
                  placeholder="e.g. MORF Saturday"
                  placeholderTextColor={colors.text.muted}
                  style={[
                    styles.input,
                    {
                      color: colors.text.primary,
                      backgroundColor: colors.surface.elevated,
                      borderColor: colors.border.hairline,
                      fontFamily: font.body,
                      fontSize: size.body,
                    },
                  ]}
                />
              </Section>

              <Section label="Mode">
                <View style={styles.segmented}>
                  <SegmentedItem
                    label="Inshore"
                    selected={mode === "inshore"}
                    onPress={() => setMode("inshore")}
                  />
                  <SegmentedItem
                    label="Distance"
                    selected={mode === "distance"}
                    onPress={() => setMode("distance")}
                  />
                </View>
              </Section>

              <Section label="Boat class">
                <PickerRow
                  value={boatClass}
                  options={(BOAT_CLASSES as string[]).map((c) => ({
                    value: c,
                    label: c,
                  }))}
                  onChange={setBoatClass}
                />
              </Section>

              <Section label="Boat (for handicap)">
                <PickerRow
                  value={boatId ?? ""}
                  options={[
                    { value: "", label: "— No boat (skip corrected time) —" },
                    ...boatOptions.map((b) => ({
                      value: b.id,
                      label: `${b.name}${b.sail_number ? ` (#${b.sail_number})` : ""}`,
                    })),
                  ]}
                  onChange={(v) => setBoatId(v || null)}
                />
                <Pressable
                  onPress={() => setUsesSpinnaker((v) => !v)}
                  style={styles.checkboxRow}
                  accessibilityRole="checkbox"
                  accessibilityState={{ checked: usesSpinnaker }}
                >
                  <View
                    style={[
                      styles.checkbox,
                      {
                        borderColor: colors.border.divider,
                        backgroundColor: usesSpinnaker
                          ? colors.accent.primary
                          : "transparent",
                      },
                    ]}
                  >
                    {usesSpinnaker ? (
                      <Ionicons name="checkmark" size={14} color={colors.text.onAccent} />
                    ) : null}
                  </View>
                  <Text
                    style={{
                      flex: 1,
                      color: colors.text.secondary,
                      fontFamily: font.body,
                      fontSize: size.small,
                    }}
                  >
                    Flying spinnaker (uses HCP/DHCP; uncheck for NSHCP/DNSHCP)
                  </Text>
                </Pressable>
              </Section>

              <Section
                label="Start (local time)"
                action={
                  startDate || startTime ? (
                    <Pressable
                      onPress={() => {
                        setStartDate("");
                        setStartTime("");
                      }}
                      hitSlop={8}
                    >
                      <Text
                        style={{
                          color: colors.accent.primary,
                          fontFamily: font.body,
                          fontSize: size.small,
                        }}
                      >
                        Clear
                      </Text>
                    </Pressable>
                  ) : null
                }
              >
                <View style={styles.startRow}>
                  <TextInput
                    value={startDate}
                    onChangeText={setStartDate}
                    placeholder="YYYY-MM-DD"
                    placeholderTextColor={colors.text.muted}
                    autoCapitalize="none"
                    autoCorrect={false}
                    keyboardType="numbers-and-punctuation"
                    style={[
                      styles.input,
                      styles.startInput,
                      {
                        color: colors.text.primary,
                        backgroundColor: colors.surface.elevated,
                        borderColor: colors.border.hairline,
                        fontFamily: font.body,
                        fontSize: size.body,
                      },
                    ]}
                    accessibilityLabel="Race date (YYYY-MM-DD)"
                  />
                  <TextInput
                    value={startTime}
                    onChangeText={setStartTime}
                    placeholder="HH:mm"
                    placeholderTextColor={colors.text.muted}
                    autoCapitalize="none"
                    autoCorrect={false}
                    keyboardType="numbers-and-punctuation"
                    style={[
                      styles.input,
                      styles.startInput,
                      {
                        color: colors.text.primary,
                        backgroundColor: colors.surface.elevated,
                        borderColor: colors.border.hairline,
                        fontFamily: font.body,
                        fontSize: size.body,
                      },
                    ]}
                    accessibilityLabel="Class start time (HH:mm, 24h)"
                  />
                </View>
              </Section>

              <Section label="MORF course preset">
                <PickerRow
                  value=""
                  options={[
                    { value: "", label: "— Pick a course to load —" },
                    ...COURSE_FAMILIES.flatMap(
                      (fam: { family: string; label: string; courses: string[] }) =>
                        fam.courses.map((id: string) => ({
                          value: id,
                          label: `${fam.label.split("—")[0].trim()} · ${id}`,
                        })),
                    ),
                  ]}
                  onChange={(v) => {
                    if (v) applyCoursePreset(v);
                  }}
                />
              </Section>

              <Section
                label={`Course (${marks.length})`}
                action={
                  <Pressable
                    onPress={() =>
                      setCoordFormat((f) => (f === "dm" ? "decimal" : "dm"))
                    }
                    hitSlop={8}
                  >
                    <Text
                      style={{
                        color: colors.accent.primary,
                        fontFamily: font.body,
                        fontSize: size.small,
                      }}
                    >
                      {coordFormat === "dm" ? "Deg-min" : "Decimal"}
                    </Text>
                  </Pressable>
                }
              >
                {marks.length === 0 ? (
                  <Text
                    style={{
                      color: colors.text.muted,
                      fontFamily: font.body,
                      fontSize: size.small,
                      lineHeight: 18,
                    }}
                  >
                    Pick a MORF course above, tap anywhere on the map to drop
                    a mark, or add one and type its lat/lon below.
                  </Text>
                ) : (
                  marks.map((m, i) => (
                    <MarkRow
                      key={i}
                      index={i}
                      mark={m}
                      format={coordFormat}
                      isFirst={i === 0}
                      isLast={i === marks.length - 1}
                      onRename={(v) => renameMark(i, v)}
                      onLat={(v) => updateLat(i, v)}
                      onLon={(v) => updateLon(i, v)}
                      onUp={() => moveUp(i)}
                      onDown={() => moveDown(i)}
                      onDelete={() => deleteMark(i)}
                    />
                  ))
                )}
                <Pressable
                  onPress={addEmptyMark}
                  style={[
                    styles.addBtn,
                    {
                      borderColor: colors.border.divider,
                    },
                  ]}
                >
                  <Ionicons
                    name="add"
                    size={16}
                    color={colors.accent.primary}
                  />
                  <Text
                    style={{
                      color: colors.accent.primary,
                      fontFamily: font.bodyMedium,
                      fontSize: size.body,
                    }}
                  >
                    Add mark manually
                  </Text>
                </Pressable>
              </Section>
            </ScrollView>
          </KeyboardAvoidingView>
        </BottomSheetView>
      </BottomSheet>

      {loading ? (
        <View style={styles.loadingOverlay}>
          <ActivityIndicator color={colors.accent.primary} />
        </View>
      ) : null}
    </View>
  );
}

// ── Sub-components ──────────────────────────────────────────────────

function Section({
  label,
  action,
  children,
}: {
  label: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  const { colors, font, size } = useTheme();
  return (
    <View style={styles.section}>
      <View style={styles.sectionHeader}>
        <Text
          style={{
            color: colors.text.muted,
            fontFamily: font.bodyMedium,
            fontSize: size.caption,
            letterSpacing: 0.6,
            textTransform: "uppercase",
          }}
        >
          {label}
        </Text>
        {action ?? null}
      </View>
      {children}
    </View>
  );
}

function SegmentedItem({
  label,
  selected,
  onPress,
}: {
  label: string;
  selected: boolean;
  onPress: () => void;
}) {
  const { colors, font, size } = useTheme();
  return (
    <Pressable
      onPress={onPress}
      style={[
        styles.segItem,
        {
          backgroundColor: selected
            ? colors.accent.primary
            : colors.surface.elevated,
          borderColor: colors.border.hairline,
        },
      ]}
      accessibilityRole="button"
      accessibilityState={{ selected }}
    >
      <Text
        style={{
          color: selected ? colors.text.onAccent : colors.text.primary,
          fontFamily: font.bodyMedium,
          fontSize: size.body,
        }}
      >
        {label}
      </Text>
    </Pressable>
  );
}

/**
 * PickerRow — tap to open a full-screen modal list of options. Native
 * <Picker> isn't bundled in Expo SDK 54 and Alert.alert tops out at
 * ~8 useful actions before it overflows the screen, so a self-rolled
 * Modal + FlatList covers both small lists (boat classes, ~8) and
 * large ones (course presets, ~80) without a new dep.
 */
function PickerRow({
  value,
  options,
  onChange,
  modalTitle = "Select",
}: {
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
  modalTitle?: string;
}) {
  const { colors, font, size } = useTheme();
  const [open, setOpen] = useState(false);
  const current = options.find((o) => o.value === value);
  const display = current?.label ?? "— Select —";

  const pick = useCallback(
    (next: string) => {
      onChange(next);
      setOpen(false);
    },
    [onChange],
  );

  return (
    <>
      <Pressable
        onPress={() => setOpen(true)}
        style={[
          styles.input,
          styles.pickerRow,
          {
            backgroundColor: colors.surface.elevated,
            borderColor: colors.border.hairline,
          },
        ]}
        accessibilityRole="button"
        accessibilityLabel={`${display} — tap to change`}
      >
        <Text
          style={{
            flex: 1,
            color: current ? colors.text.primary : colors.text.muted,
            fontFamily: font.body,
            fontSize: size.body,
          }}
          numberOfLines={1}
        >
          {display}
        </Text>
        <Ionicons name="chevron-down" size={16} color={colors.text.muted} />
      </Pressable>

      <Modal
        visible={open}
        animationType="slide"
        presentationStyle="pageSheet"
        onRequestClose={() => setOpen(false)}
      >
        <View
          style={[
            styles.modalRoot,
            { backgroundColor: colors.surface.page },
          ]}
        >
          <View
            style={[
              styles.modalHeader,
              {
                backgroundColor: colors.surface.sheet,
                borderBottomColor: colors.border.hairline,
              },
            ]}
          >
            <Text
              style={{
                color: colors.text.primary,
                fontFamily: font.displaySemibold,
                fontSize: size.subtitle,
              }}
            >
              {modalTitle}
            </Text>
            <Pressable onPress={() => setOpen(false)} hitSlop={10}>
              <Text
                style={{
                  color: colors.accent.primary,
                  fontFamily: font.bodySemibold,
                  fontSize: size.body,
                }}
              >
                Done
              </Text>
            </Pressable>
          </View>
          <FlatList
            data={options}
            keyExtractor={(o, i) => `${o.value}-${i}`}
            renderItem={({ item }) => {
              const selected = item.value === value;
              return (
                <Pressable
                  onPress={() => pick(item.value)}
                  style={({ pressed }) => [
                    styles.modalRow,
                    {
                      backgroundColor: pressed
                        ? colors.surface.elevated
                        : colors.surface.sheet,
                      borderBottomColor: colors.border.hairline,
                    },
                  ]}
                >
                  <Text
                    style={{
                      flex: 1,
                      color: colors.text.primary,
                      fontFamily: selected ? font.bodySemibold : font.body,
                      fontSize: size.body,
                    }}
                  >
                    {item.label}
                  </Text>
                  {selected ? (
                    <Ionicons
                      name="checkmark"
                      size={18}
                      color={colors.accent.primary}
                    />
                  ) : null}
                </Pressable>
              );
            }}
          />
        </View>
      </Modal>
    </>
  );
}

/**
 * MarkRow — name + lat + lon + up/down/delete controls. Local string
 * state for the coord inputs so partial typing doesn't immediately
 * re-format. Commits on blur.
 */
function MarkRow({
  index,
  mark,
  format,
  isFirst,
  isLast,
  onRename,
  onLat,
  onLon,
  onUp,
  onDown,
  onDelete,
}: {
  index: number;
  mark: EditMark;
  format: CoordFormat;
  isFirst: boolean;
  isLast: boolean;
  onRename: (v: string) => void;
  onLat: (v: number | null) => void;
  onLon: (v: number | null) => void;
  onUp: () => void;
  onDown: () => void;
  onDelete: () => void;
}) {
  const { colors, font, size } = useTheme();

  const fmtLat = useCallback(
    (v: number | null): string => {
      if (!Number.isFinite(v)) return "";
      return format === "dm" ? formatLatInput(v as number) : formatDecimal(v as number);
    },
    [format],
  );
  const fmtLon = useCallback(
    (v: number | null): string => {
      if (!Number.isFinite(v)) return "";
      return format === "dm" ? formatLonInput(v as number) : formatDecimal(v as number);
    },
    [format],
  );

  const [latStr, setLatStr] = useState(fmtLat(mark.lat));
  const [lonStr, setLonStr] = useState(fmtLon(mark.lon));

  useEffect(() => {
    setLatStr(fmtLat(mark.lat));
  }, [mark.lat, fmtLat]);
  useEffect(() => {
    setLonStr(fmtLon(mark.lon));
  }, [mark.lon, fmtLon]);

  const commit = (
    str: string,
    current: number | null,
    fmt: (v: number | null) => string,
    setStr: (v: string) => void,
    onCommit: (v: number | null) => void,
  ) => {
    const trimmed = str.trim();
    if (trimmed === "") {
      onCommit(null);
      setStr("");
      return;
    }
    const v = parseCoord(trimmed);
    if (Number.isFinite(v)) {
      onCommit(v as number);
      setStr(fmt(v as number));
    } else {
      // Junk → revert display to last good value (or empty if none).
      setStr(fmt(current));
    }
  };

  const unplaced =
    !Number.isFinite(mark.lat) || !Number.isFinite(mark.lon);

  const placeholderLat = format === "dm" ? "41 51.17 N" : "41.85283";
  const placeholderLon = format === "dm" ? "87 33.41 W" : "-87.55683";

  return (
    <View
      style={[
        styles.markRow,
        { borderColor: colors.border.hairline },
      ]}
    >
      <View style={styles.markRowTop}>
        <View
          style={[
            styles.markIdx,
            {
              backgroundColor: unplaced
                ? colors.text.muted
                : colors.text.primary,
            },
          ]}
        >
          <Text
            style={{
              color: colors.text.inverse,
              fontFamily: font.bodyBold,
              fontSize: size.small,
            }}
          >
            {index + 1}
          </Text>
        </View>
        <TextInput
          value={mark.name}
          onChangeText={onRename}
          style={[
            styles.markName,
            {
              color: colors.text.primary,
              fontStyle: unplaced ? "italic" : "normal",
              fontFamily: font.body,
              fontSize: size.body,
            },
          ]}
        />
        <Pressable
          onPress={onUp}
          disabled={isFirst}
          hitSlop={6}
          style={styles.iconBtn}
        >
          <Ionicons
            name="arrow-up"
            size={16}
            color={isFirst ? colors.text.muted : colors.text.secondary}
          />
        </Pressable>
        <Pressable
          onPress={onDown}
          disabled={isLast}
          hitSlop={6}
          style={styles.iconBtn}
        >
          <Ionicons
            name="arrow-down"
            size={16}
            color={isLast ? colors.text.muted : colors.text.secondary}
          />
        </Pressable>
        <Pressable onPress={onDelete} hitSlop={6} style={styles.iconBtn}>
          <Ionicons name="close" size={16} color={colors.accent.recording} />
        </Pressable>
      </View>
      <View style={styles.markRowBottom}>
        <TextInput
          value={latStr}
          onChangeText={setLatStr}
          onBlur={() => commit(latStr, mark.lat, fmtLat, setLatStr, onLat)}
          placeholder={placeholderLat}
          placeholderTextColor={colors.text.muted}
          autoCapitalize="characters"
          autoCorrect={false}
          keyboardType="numbers-and-punctuation"
          style={[
            styles.input,
            styles.coordInput,
            {
              color: colors.text.primary,
              backgroundColor: colors.surface.elevated,
              borderColor: unplaced
                ? colors.text.muted
                : colors.border.hairline,
              borderStyle: unplaced ? "dashed" : "solid",
              fontFamily: font.body,
              fontSize: size.small,
            },
          ]}
        />
        <TextInput
          value={lonStr}
          onChangeText={setLonStr}
          onBlur={() => commit(lonStr, mark.lon, fmtLon, setLonStr, onLon)}
          placeholder={placeholderLon}
          placeholderTextColor={colors.text.muted}
          autoCapitalize="characters"
          autoCorrect={false}
          keyboardType="numbers-and-punctuation"
          style={[
            styles.input,
            styles.coordInput,
            {
              color: colors.text.primary,
              backgroundColor: colors.surface.elevated,
              borderColor: unplaced
                ? colors.text.muted
                : colors.border.hairline,
              borderStyle: unplaced ? "dashed" : "solid",
              fontFamily: font.body,
              fontSize: size.small,
            },
          ]}
        />
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  topBar: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 16,
    paddingVertical: 12,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  mapWrap: { flex: 1 },
  scrollContent: {
    paddingHorizontal: 20,
    paddingTop: 4,
    paddingBottom: 60,
  },
  errorBanner: {
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 8,
    marginBottom: 16,
  },
  section: { marginBottom: 20 },
  sectionHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 8,
  },
  input: {
    paddingHorizontal: 12,
    paddingVertical: 10,
    borderRadius: 8,
    borderWidth: StyleSheet.hairlineWidth,
  },
  pickerRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  segmented: {
    flexDirection: "row",
    gap: 8,
  },
  segItem: {
    flex: 1,
    paddingVertical: 10,
    borderRadius: 8,
    borderWidth: StyleSheet.hairlineWidth,
    alignItems: "center",
  },
  checkboxRow: {
    flexDirection: "row",
    alignItems: "center",
    gap: 10,
    marginTop: 10,
  },
  checkbox: {
    width: 20,
    height: 20,
    borderRadius: 4,
    borderWidth: 1.5,
    alignItems: "center",
    justifyContent: "center",
  },
  startRow: {
    flexDirection: "row",
    gap: 10,
  },
  startInput: { flex: 1 },
  markRow: {
    paddingVertical: 10,
    borderTopWidth: StyleSheet.hairlineWidth,
  },
  markRowTop: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
  },
  markRowBottom: {
    flexDirection: "row",
    gap: 8,
    marginTop: 8,
  },
  markIdx: {
    width: 24,
    height: 24,
    borderRadius: 12,
    alignItems: "center",
    justifyContent: "center",
  },
  markName: {
    flex: 1,
    paddingVertical: 4,
  },
  iconBtn: {
    width: 28,
    height: 28,
    borderRadius: 14,
    alignItems: "center",
    justifyContent: "center",
  },
  coordInput: { flex: 1 },
  modalRoot: { flex: 1 },
  modalHeader: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingHorizontal: 20,
    paddingVertical: 14,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  modalRow: {
    flexDirection: "row",
    alignItems: "center",
    paddingHorizontal: 20,
    paddingVertical: 14,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  addBtn: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "center",
    gap: 6,
    paddingVertical: 10,
    borderRadius: 8,
    borderWidth: StyleSheet.hairlineWidth,
    borderStyle: "dashed",
    marginTop: 10,
  },
  loadingOverlay: {
    ...StyleSheet.absoluteFillObject,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: "rgba(0,0,0,0.15)",
  },
});
