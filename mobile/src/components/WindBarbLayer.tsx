// WindBarbLayer.tsx — true meteorological wind barbs.
//
// Renders proper wind barbs (shaft + flags + pennants) as Mapbox
// LineLayer features. The barb geometry is pre-computed in JS by
// `buildAllBarbFeatures` against the viewport's wind grid. Each barb
// becomes 1..N LineString features depending on speed (1 shaft + 0..N
// flag/pennant lines).
//
// Why LineLayer instead of SymbolLayer:
//   * Symbol icons require image registration. SVG data-URLs are
//     rejected by Android's native bitmap loader (silently — no
//     error, just empty rendering).
//   * Symbol text glyph layout doesn't easily compose barb geometry
//     (variable flag count per feature).
//   * LineLayer is the most fundamental Mapbox primitive — supported
//     everywhere, rotates with the map automatically, and we can
//     style line colour by feature property via a step expression.
//
// Pairs with a small CircleLayer for "calm" stations (<5kt), which
// have no barb shaft — just a tiny dot so the user sees that wind
// data is present where there's no breeze.

import {
  CircleLayer,
  LineLayer,
  ShapeSource,
} from "@rnmapbox/maps";
import { useMemo } from "react";

import {
  buildAllBarbFeatures,
  calmStationFeatures,
} from "../lib/barbGeometry";

type WindFeature = {
  type: "Feature";
  geometry: { type: "Point"; coordinates: [number, number] };
  properties: { bucket: number; dir: number };
};

type Props = {
  features: WindFeature[];
  visible: boolean;
};

const BARB_SOURCE = "wind-barbs-src";
const CALM_SOURCE = "wind-calm-src";

export function WindBarbLayer({ features, visible }: Props) {
  // Pre-compute barb geometry every render — cheap (linear in feature
  // count, typically 10-100 points per viewport). Memoised so identical
  // viewports skip the rebuild.
  const barbFeatures = useMemo(() => buildAllBarbFeatures(features), [features]);
  const calmFeatures = useMemo(() => calmStationFeatures(features), [features]);

  // Don't early-return on empty features: returning null unmounts the
  // ShapeSource / LineLayer, and on the next render Mapbox sometimes
  // doesn't re-add them cleanly (manifests as "tap the layers FAB
  // twice to see anything"). Render the source with an empty feature
  // collection instead — the SDK redraws it efficiently.
  if (!visible) return null;

  // Speed-bucket → colour ramp. Hotter than the previous version so
  // light air still reads against both blue water and beige land tiles.
  // Black for light air specifically because navy-blue-on-blue-water
  // (the prior 5-9 kt colour) was invisible on Lake Michigan.
  const colorRamp: any[] = [
    "step",
    ["get", "bucket"],
    "#475569",   // <5 kt: dark slate (calm — only used by the casing)
    5, "#0f172a",  // 5–9 kt: near-black, max contrast on any base map
    10, "#1d4e89", // 10–14 kt: deep blue
    15, "#22a06b", // 15–19 kt: green
    20, "#f4b860", // 20–24 kt: amber
    25, "#ef6a32", // 25–29 kt: orange
    30, "#d6361b", // 30+ kt: red
  ];

  // Don't early-render the calm-station CircleLayer with calmFeatures.length === 0,
  // for the same reason the parent doesn't short-circuit on empty barbs:
  // unmounting the source on a toggle blinks the layer off and only the
  // next viewport-change re-renders it. Always render the source.
  const calmCollection = {
    type: "FeatureCollection" as const,
    features: calmFeatures,
  };
  const barbCollection = {
    type: "FeatureCollection" as const,
    features: barbFeatures,
  };

  return (
    <>
      {/* Barbs — shaft, flags, pennants — for stations with wind.
          Drawn in two passes: a thick white casing UNDER a coloured
          stroke. The casing gives every barb a high-contrast outline
          regardless of map base (water, land, satellite later). Same
          trick we use for the route polyline. */}
      <ShapeSource id={BARB_SOURCE} shape={barbCollection}>
        <LineLayer
          id="wind-barbs-casing"
          style={{
            lineColor: "#ffffff",
            lineWidth: 5,
            lineCap: "round",
            lineJoin: "round",
            lineOpacity: 0.85,
          }}
        />
        <LineLayer
          id="wind-barbs"
          style={{
            lineColor: colorRamp,
            lineWidth: 3,
            lineCap: "round",
            lineJoin: "round",
          }}
        />
      </ShapeSource>

      {/* Calm stations — small bright dots with a white halo so they
          still register against a busy base map. */}
      <ShapeSource id={CALM_SOURCE} shape={calmCollection}>
        <CircleLayer
          id="wind-calm"
          style={{
            circleRadius: 4,
            circleColor: "#475569",
            circleStrokeColor: "#ffffff",
            circleStrokeWidth: 2,
          }}
        />
      </ShapeSource>
    </>
  );
}
