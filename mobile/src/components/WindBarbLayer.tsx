// WindBarbLayer.tsx — Mapbox symbol layer rendering wind barbs.
//
// Loads the bucketed barb SVGs (one per 5kt step) into the Mapbox style
// at mount via Images, then renders a SymbolLayer whose `iconImage`
// expression picks the right bucket per feature. Features are computed
// in the parent against the current viewport (see windBarbViewport.ts).
//
// Why symbols, not native polylines: rotation via icon-rotate is GPU-
// accelerated and we get one draw call for the whole layer regardless
// of feature count. Drawing each barb as a series of LineLayer features
// would be O(features) draw calls — slow at any meaningful density.

import { useMemo } from "react";
import { Images, ShapeSource, SymbolLayer } from "@rnmapbox/maps";

import { generateBarbImages } from "@sailline/shared";

type WindFeature = {
  type: "Feature";
  geometry: { type: "Point"; coordinates: [number, number] };
  properties: { bucket: number; dir: number };
};

type Props = {
  features: WindFeature[];
  visible: boolean;
};

const SOURCE_ID = "wind-barb-src";

export function WindBarbLayer({ features, visible }: Props) {
  // Pre-generate once; the map cache will dedupe across re-renders.
  const images = useMemo(() => generateBarbImages(), []);

  if (!visible || features.length === 0) return null;

  return (
    <>
      <Images images={images} />
      <ShapeSource
        id={SOURCE_ID}
        shape={{ type: "FeatureCollection", features }}
      >
        <SymbolLayer
          id="wind-barbs"
          style={{
            iconImage: ["concat", "barb-", ["get", "bucket"]],
            iconAllowOverlap: true,
            iconIgnorePlacement: true,
            iconRotate: ["get", "dir"],
            iconRotationAlignment: "map",
            iconSize: 0.6,
          }}
        />
      </ShapeSource>
    </>
  );
}
