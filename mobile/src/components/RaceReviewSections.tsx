// RaceReviewSections.tsx — the post-race stats + AI recap sections.
//
// Extracted from app/(app)/race-review/[id].tsx (2026-07-02) so the new
// map-first Debrief screen (app/(app)/debrief/[id].tsx) can render the
// same content under its map without duplicating the cards. Both screens
// own their useRaceStats(raceId) call and pass the results down — this
// component is purely presentational and expects to live inside the
// caller's ScrollView.
//
// The stats render as soon as they load; the AI Coach card has three
// states driven by useRaceStats:
//   generating  — job still producing the recap (the hook polls every
//                 20 s for up to 10 min).
//   ready        — recap + tips present.
//   unavailable  — poll budget spent, or the job finished without a
//                  recap (no wind / no key / track not scoreable). Stats
//                  stay; the card offers Retry (re-arms the poll budget).

import { useMemo } from "react";
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  View,
} from "react-native";
import { Ionicons } from "@expo/vector-icons";

import { useTheme } from "../theme/ThemeProvider";
import type { AiSummary, RaceStats } from "../api/raceStats";
import type { RaceStatsPhase } from "../hooks/useRaceStats";

function fmtElapsed(totalSeconds: number): string {
  const s = Math.max(0, Math.round(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function fmtNm(distanceM: number): string {
  return (distanceM / 1852).toFixed(2);
}

function cardinal(deg: number): string {
  const dirs = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
  ];
  return dirs[Math.round((((deg % 360) + 360) % 360) / 22.5) % 16];
}

type Props = {
  data: RaceStats | null;
  phase: RaceStatsPhase;
  error: string | null;
  refresh: () => Promise<void>;
};

export function RaceReviewSections({ data, phase, error, refresh }: Props) {
  const { colors, font, size } = useTheme();

  const aiTint = `${colors.accent.primary}14`;
  const aiBorder = `${colors.accent.primary}33`;

  const heroCards = useMemo(() => {
    const s = data?.stats;
    if (!s) return null;
    return [
      { label: "Elapsed", value: fmtElapsed(s.elapsed_s), unit: "" },
      { label: "Distance", value: fmtNm(s.distance_m), unit: " nm" },
      { label: "Avg speed", value: s.avg_sog_kt.toFixed(1), unit: " kt" },
    ];
  }, [data]);

  const perf = data?.performance_summary ?? null;
  const heel = data?.heel_summary ?? null;
  const wind = data?.wind ?? null;
  const legs = data?.stats?.legs ?? [];

  return (
    <>
      {/* First-load spinner */}
      {phase === "loading" ? (
        <View style={styles.center}>
          <ActivityIndicator color={colors.accent.primary} />
        </View>
      ) : null}

      {/* Hard error with nothing to show */}
      {phase === "error" && !data ? (
        <View style={[styles.card, cardStyle(colors)]}>
          <Text style={{ color: colors.text.primary, fontFamily: font.bodySemibold, fontSize: size.bodyLg }}>
            Couldn't load this race
          </Text>
          <Text style={{ color: colors.text.muted, fontFamily: font.body, fontSize: size.body, marginTop: 6 }}>
            {error}
          </Text>
          <RetryButton onPress={refresh} colors={colors} font={font} size={size} />
        </View>
      ) : null}

      {/* Hero metric cards */}
      {heroCards ? (
        <View style={styles.metricRow}>
          {heroCards.map((c) => (
            <View key={c.label} style={[styles.metric, { backgroundColor: colors.surface.elevated }]}>
              <Text style={{ color: colors.text.muted, fontFamily: font.bodyMedium, fontSize: size.caption, letterSpacing: 0.3 }}>
                {c.label.toUpperCase()}
              </Text>
              <Text style={{ color: colors.text.primary, fontFamily: font.tabular, fontSize: 24, marginTop: 3 }}>
                {c.value}
                <Text style={{ fontSize: size.small, color: colors.text.secondary }}>{c.unit}</Text>
              </Text>
            </View>
          ))}
        </View>
      ) : null}

      {/* AI Coach card */}
      {data ? (
        <View style={[styles.card, { backgroundColor: aiTint, borderColor: aiBorder, borderWidth: StyleSheet.hairlineWidth }]}>
          <View style={styles.cardHead}>
            <Ionicons name="sparkles" size={14} color={colors.accent.primary} />
            <Text style={{ color: colors.accent.primary, fontFamily: font.bodySemibold, fontSize: size.caption, letterSpacing: 0.4 }}>
              AI COACH
            </Text>
          </View>

          {phase === "ready" && data.ai_summary ? (
            data.ai_summary.summary ? (
              <AnalysisSections
                summary={data.ai_summary}
                colors={colors}
                font={font}
                size={size}
              />
            ) : (
              <>
                {/* Legacy v3 shape — shown until the race regenerates under v4 */}
                <Text style={{ color: colors.text.primary, fontFamily: font.body, fontSize: size.body, lineHeight: 21, marginTop: 6 }}>
                  {data.ai_summary.recap}
                </Text>
                {data.ai_summary.tips?.map((tip, i) => (
                  <Text
                    key={i}
                    style={{ color: colors.accent.primary, fontFamily: font.bodyMedium, fontSize: size.body, lineHeight: 20, marginTop: 8 }}
                  >
                    {"› "}
                    <Text style={{ color: colors.text.secondary }}>{tip}</Text>
                  </Text>
                ))}
              </>
            )
          ) : phase === "generating" ? (
            <View style={{ flexDirection: "row", alignItems: "center", gap: 8, marginTop: 8 }}>
              <ActivityIndicator size="small" color={colors.accent.primary} />
              <Text style={{ color: colors.accent.primary, fontFamily: font.bodyMedium, fontSize: size.body }}>
                Generating race summary…
              </Text>
            </View>
          ) : (
            <>
              <Text style={{ color: colors.text.secondary, fontFamily: font.body, fontSize: size.body, lineHeight: 21, marginTop: 6 }}>
                Analysis isn't available for this race yet — the recap needs wind data over the race window, which wasn't on hand.
              </Text>
              <RetryButton onPress={refresh} colors={colors} font={font} size={size} />
            </>
          )}
        </View>
      ) : null}

      {/* Performance vs polar */}
      {perf && (perf.avg_speed_ratio != null || perf.avg_vmg_efficiency != null) ? (
        <View style={[styles.card, cardStyle(colors)]}>
          <CardLabel text="PERFORMANCE vs POLAR" colors={colors} font={font} size={size} />
          {perf.avg_vmg_efficiency != null ? (
            <Bar label="Upwind VMG" pct={perf.avg_vmg_efficiency} colors={colors} font={font} size={size} />
          ) : null}
          {perf.avg_speed_ratio != null ? (
            <Bar label="Boatspeed" pct={perf.avg_speed_ratio} colors={colors} font={font} size={size} />
          ) : null}
          <Text style={{ color: colors.text.muted, fontFamily: font.body, fontSize: size.caption, marginTop: 8 }}>
            {Math.round(perf.pct_time_on_target * 100)}% of the time within 5% of polar boatspeed
          </Text>
        </View>
      ) : null}

      {/* Leg splits */}
      {legs.length > 0 ? (
        <View style={[styles.card, cardStyle(colors)]}>
          <CardLabel text="LEG SPLITS" colors={colors} font={font} size={size} />
          {legs.map((leg) => (
            <View
              key={leg.leg_index}
              style={[styles.legRow, { borderBottomColor: colors.border.hairline }]}
            >
              <Text style={{ color: colors.text.primary, fontFamily: font.body, fontSize: size.body }} numberOfLines={1}>
                {leg.from_label} → {leg.to_label}
              </Text>
              <Text style={{ color: colors.text.secondary, fontFamily: font.tabular, fontSize: size.body }}>
                {fmtElapsed(leg.elapsed_s)}
              </Text>
            </View>
          ))}
        </View>
      ) : null}

      {/* Heel + Wind */}
      {heel || wind ? (
        <View style={styles.metricRow}>
          {heel ? (
            <View style={[styles.card, cardStyle(colors), styles.halfCard]}>
              <CardLabel text="HEEL" colors={colors} font={font} size={size} />
              <Text style={{ color: colors.text.primary, fontFamily: font.bodyMedium, fontSize: size.bodyLg, marginTop: 4 }}>
                {heel.avg_heel_abs_deg.toFixed(0)}° avg
              </Text>
              <Text style={{ color: colors.text.muted, fontFamily: font.body, fontSize: size.small, marginTop: 1 }}>
                {heel.max_heel_abs_deg.toFixed(0)}° max
              </Text>
            </View>
          ) : null}
          {wind ? (
            <View style={[styles.card, cardStyle(colors), styles.halfCard]}>
              <CardLabel text="WIND" colors={colors} font={font} size={size} />
              <Text style={{ color: colors.text.primary, fontFamily: font.bodyMedium, fontSize: size.bodyLg, marginTop: 4 }}>
                {wind.mean_dir_deg != null ? cardinal(wind.mean_dir_deg) + " " : ""}
                {wind.mean_speed_kt != null ? wind.mean_speed_kt.toFixed(0) : "—"}
                {wind.max_speed_kt != null ? `–${wind.max_speed_kt.toFixed(0)}` : ""} kt
              </Text>
            </View>
          ) : null}
        </View>
      ) : null}

      {/* No-track empty state */}
      {data && !data.stats ? (
        <View style={[styles.card, cardStyle(colors)]}>
          <Text style={{ color: colors.text.secondary, fontFamily: font.body, fontSize: size.body }}>
            No track was recorded for this race, so there are no stats to show.
          </Text>
        </View>
      ) : null}
    </>
  );
}

// ─── Small presentational helpers ─────────────────────────────────────

function AnalysisSections({
  summary, colors, font, size,
}: { summary: AiSummary; colors: any; font: any; size: any }) {
  const worked = summary.what_worked ?? [];
  const cost = summary.what_cost ?? [];
  const playbook = summary.playbook ?? null;
  const totalLoss = summary.total_identifiable_loss_s;

  const head = (text: string) => (
    <Text
      style={{
        color: colors.text.muted,
        fontFamily: font.bodySemibold,
        fontSize: size.caption,
        letterSpacing: 0.4,
        marginTop: 14,
      }}
    >
      {text}
    </Text>
  );

  return (
    <>
      <Text style={{ color: colors.text.primary, fontFamily: font.body, fontSize: size.body, lineHeight: 21, marginTop: 6 }}>
        {summary.summary}
      </Text>

      {worked.length ? (
        <>
          {head("WHAT WORKED")}
          {worked.map((t, i) => (
            <Text
              key={i}
              style={{ color: colors.accent.primary, fontFamily: font.bodyMedium, fontSize: size.body, lineHeight: 20, marginTop: 8 }}
            >
              {"› "}
              <Text style={{ color: colors.text.secondary }}>{t}</Text>
            </Text>
          ))}
        </>
      ) : null}

      {cost.length ? (
        <>
          {head("WHAT COST US")}
          {cost.map((c, i) => (
            <Text
              key={i}
              style={{ color: colors.text.secondary, fontFamily: font.body, fontSize: size.body, lineHeight: 20, marginTop: 8 }}
            >
              <Text style={{ fontFamily: font.bodySemibold, color: colors.accent.primary }}>
                {c.tag}
              </Text>
              {"  "}
              {c.text}
              {c.cost_s != null ? (
                <Text style={{ fontFamily: font.bodySemibold }}>
                  {`  ~${Math.round(c.cost_s)}s`}
                </Text>
              ) : null}
            </Text>
          ))}
          {totalLoss != null ? (
            <Text style={{ color: colors.text.primary, fontFamily: font.bodySemibold, fontSize: size.body, marginTop: 10 }}>
              Total identifiable loss: ~{Math.round(totalLoss)}s
            </Text>
          ) : null}
        </>
      ) : null}

      {playbook?.directives?.length ? (
        <>
          {head("SAME-CONDITIONS PLAYBOOK")}
          {playbook.signature_text ? (
            <Text style={{ color: colors.text.muted, fontFamily: font.body, fontSize: size.caption, fontStyle: "italic", marginTop: 6 }}>
              {playbook.signature_text}
            </Text>
          ) : null}
          {playbook.directives.map((d, i) => (
            <Text
              key={i}
              style={{ color: colors.text.secondary, fontFamily: font.body, fontSize: size.body, lineHeight: 20, marginTop: 8 }}
            >
              <Text style={{ fontFamily: font.bodySemibold, color: colors.accent.primary }}>
                {i + 1}.
              </Text>
              {" "}
              {d}
            </Text>
          ))}
        </>
      ) : null}
    </>
  );
}

function cardStyle(colors: any) {
  return {
    backgroundColor: colors.surface.floating,
    borderColor: colors.border.hairline,
    borderWidth: StyleSheet.hairlineWidth,
  };
}

function CardLabel({ text, colors, font, size }: any) {
  return (
    <Text style={{ color: colors.text.muted, fontFamily: font.bodyMedium, fontSize: size.caption, letterSpacing: 0.4 }}>
      {text}
    </Text>
  );
}

function Bar({ label, pct, colors, font, size }: any) {
  const clamped = Math.max(0, Math.min(1.2, pct));
  const pctNum = Math.min(100, clamped * 100);
  const widthPct = `${pctNum}%` as `${number}%`;
  const good = pct >= 0.85;
  const barColor = good ? colors.accent.success : colors.accent.warning;
  return (
    <View style={{ marginTop: 8 }}>
      <View style={{ flexDirection: "row", justifyContent: "space-between", marginBottom: 4 }}>
        <Text style={{ color: colors.text.secondary, fontFamily: font.body, fontSize: size.body }}>{label}</Text>
        <Text style={{ color: barColor, fontFamily: font.bodySemibold, fontSize: size.body }}>
          {Math.round(pct * 100)}%
        </Text>
      </View>
      <View style={{ height: 6, borderRadius: 4, backgroundColor: colors.surface.elevated, overflow: "hidden" }}>
        <View style={{ height: "100%", width: widthPct, backgroundColor: barColor }} />
      </View>
    </View>
  );
}

function RetryButton({ onPress, colors, font, size }: any) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityLabel="Retry analysis"
      style={({ pressed }) => [
        styles.retry,
        { borderColor: colors.border.divider, opacity: pressed ? 0.7 : 1 },
      ]}
    >
      <Ionicons name="refresh" size={14} color={colors.text.primary} />
      <Text style={{ color: colors.text.primary, fontFamily: font.bodyMedium, fontSize: size.body }}>
        Retry analysis
      </Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  center: { paddingVertical: 60, alignItems: "center" },
  metricRow: { flexDirection: "row", gap: 8, marginTop: 12 },
  metric: { flex: 1, borderRadius: 10, paddingVertical: 10, paddingHorizontal: 11 },
  card: { borderRadius: 12, padding: 13, marginTop: 12 },
  halfCard: { flex: 1, marginTop: 0 },
  cardHead: { flexDirection: "row", alignItems: "center", gap: 6 },
  legRow: {
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
    paddingVertical: 8,
    borderBottomWidth: StyleSheet.hairlineWidth,
  },
  retry: {
    flexDirection: "row",
    alignItems: "center",
    alignSelf: "flex-start",
    gap: 6,
    marginTop: 12,
    paddingVertical: 8,
    paddingHorizontal: 12,
    borderRadius: 999,
    borderWidth: StyleSheet.hairlineWidth,
  },
});
