// ActualRouteLayer.tsx — live polyline of the recorder's GPS breadcrumb.
//
// Renders the recorder's captured `points` as a single Mapbox
// LineString so the sailor can see, in real time, the track they've
// actually sailed vs. the computed (planned) route already drawn by
// MapCanvas. Sits ABOVE the computed route in the layer stack so the
// actual line dominates visually — the planned route is reference, the
// actual line is truth.
//
// Performance:
//   * Hard cap at MAX_POINTS samples (keeps the source small even on
//     a 3-hour race at 1 Hz = 10k+ points). We keep the LAST N points
//     so the recent portion of the track stays high-fidelity. The
//     dropped early section reappears as a coarsened polyline only if
//     a future commit adds Douglas-Peucker; today, very old fixes
//     simply vanish off the back. Flagged below as tech debt.
//   * The GeoJSON object is memoised by point count, so a fresh point
//     arriving (1 Hz cadence) does ONE allocation per second.
//
// Why a casing + stroke (same pattern as the route polyline above):
//   * High-contrast white outline reads against light water, dark
//     water, and the muted greys of our base style.
//   * The colour stroke makes it instantly distinguishable from the
//     computed route's blue: actual = recording accent (red), planned =
//     blue. Two semantically different lines, two colours.
//
// 2026-06-03 B3 — added per the mobile-fixes plan.

import { useMemo } from "react";
import { LineLayer, ShapeSource } from "@rnmapbox/maps";

import { useTheme } from "../theme/ThemeProvider";
import type { LocalPoint } from "../recorder/backgroundGeolocation";

const SOURCE_ID = "actual-route-src";
const MAX_POINTS = 5_000;

type Props = {
  /** `readonly` because RecorderContext exposes its points array as
   *  `ReadonlyArray<LocalPoint>` to keep callers from mutating the
   *  recorder's internal state. We only read here. */
  points: readonly LocalPoint[];
  /** Hide the layer without unmounting (parity with WindBarbLayer's
   *  visibility handling — see comment there about toggle blink). */
  visible: boolean;
};

export function ActualRouteLayer({ points, visible }: Props) {
  const { colors } = useTheme();

  // Build a FeatureCollection holding one LineString of the most-recent
  // MAX_POINTS fixes. Only re-run when the count changes — every 1 s
  // during recording is fine; the parent re-renders at the same
  // cadence anyway when `points` grows. Equality below covers both
  // additions (recording) and full resets (race switch / clear).
  const collection = useMemo(() => {
    if (points.length === 0) {
      return {
        type: "FeatureCollection" as const,
        features: [],
      };
    }
    const window =
      points.length > MAX_POINTS ? points.slice(points.length - MAX_POINTS) : points;
    const coords: [number, number][] = window.map((p) => [p.lon, p.lat]);
    return {
      type: "FeatureCollection" as const,
      features: [
        {
          type: "Feature" as const,
          geometry: {
            type: "LineString" as const,
            coordinates: coords,
          },
          properties: {},
        },
      ],
    };
  }, [points]);

  if (!visible) return null;

  return (
    <ShapeSource id={SOURCE_ID} shape={collection}>
      <LineLayer
        id="actual-route-casing"
        style={{
          lineColor: "#ffffff",
          lineWidth: 6,
          lineCap: "round",
          lineJoin: "round",
          lineOpacity: 0.9,
        }}
      />
      <LineLayer
        id="actual-route-stroke"
        style={{
          // Recording accent reads as "this is the live, currently-being-
          // captured line" — same colour as the LIVE chip and the Stop
          // button, so the user's eye groups them together.
          lineColor: colors.accent.recording,
          lineWidth: 3.5,
          lineCap: "round",
          lineJoin: "round",
        }}
      />
    </ShapeSource>
  );
}
