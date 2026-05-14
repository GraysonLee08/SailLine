// frontend/src/hooks/useRouteFreshnessCheck.js
//
// Quantitative wind-drift check (Option B) for the T-5 pre-start banner.
//
// Premise: when the user computes a route, the backend stamps the
// wind-at-start (direction + speed in knots) onto `routing.meta`. Those
// are the values the route's tactics are anchored to. The map's
// `baseWeather` source-of-truth, meanwhile, is the freshest forecast
// (typically refreshed hourly). If the two have diverged materially,
// the cached route may no longer reflect the right strategy — surface
// a recompute prompt.
//
// "Materially" thresholds:
//   - direction:  > 15° smallest-circle delta
//   - speed:      > 3 knots absolute delta
// Either trigger marks the route stale.
//
// This runs entirely client-side from data already loaded in MapView —
// no extra HTTP call. The trade-off: `baseWeather` is the current
// valid_time, not race_start, so the comparison is "now's wind" vs
// "route's wind at gun time." Within the T-5 → T+0 window the gap is
// small (≤5 min of forecast time) and well below the typical HRRR step,
// so it's a fair signal. We're optimising for "user about to start, is
// my plan still defensible" — not for high-precision verification.
//
// Returned shape:
//   {
//     ready:       both inputs present → check is meaningful
//     stale:       boolean
//     deltaDirDeg: signed-magnitude smallest-circle delta (0..180)
//     deltaSpeedKt:absolute delta
//     currentDir:  computed from baseWeather sample at start mark
//     currentSpd:  ditto
//     routeDir:    pulled from routing.meta
//     routeSpd:    ditto
//   }
//
// `ready=false` whenever any input is missing (no route yet, weather
// still loading, start mark off-grid). Consumers should treat
// `!ready` as "no banner."

import { useMemo } from "react";
import { bilerpUV, uvToSpeedDir } from "../lib/windBarb";

// Empirical thresholds — see the file header for rationale. Bumped to
// per-export constants so tests / future tuning can override without
// chasing magic numbers.
export const STALE_DIR_DEG = 15;
export const STALE_SPEED_KT = 3;

/**
 * @param {object} args
 * @param {object|null} args.routeMeta   `routing.meta` from useRouting
 * @param {object|null} args.baseWeather Latest baseWeather payload
 * @param {object|null} args.startMark   `{lat, lon}` of the race's marks[0]
 * @returns {{ready: boolean, stale: boolean, deltaDirDeg: number,
 *            deltaSpeedKt: number, currentDir: number|null,
 *            currentSpd: number|null, routeDir: number|null,
 *            routeSpd: number|null}}
 */
export function useRouteFreshnessCheck({ routeMeta, baseWeather, startMark }) {
  return useMemo(() => {
    const empty = {
      ready: false,
      stale: false,
      deltaDirDeg: 0,
      deltaSpeedKt: 0,
      currentDir: null,
      currentSpd: null,
      routeDir: null,
      routeSpd: null,
    };

    if (!routeMeta || !baseWeather || !startMark) return empty;

    const routeDir = routeMeta.start_wind_dir_deg;
    const routeSpd = routeMeta.start_wind_speed_kt;
    if (!Number.isFinite(routeDir) || !Number.isFinite(routeSpd)) {
      // Older cached route from before the meta fields existed — can't
      // compute a delta. Treat as "fresh enough" rather than nagging.
      return { ...empty, routeDir: null, routeSpd: null };
    }

    const sample = bilerpUV(baseWeather, startMark.lat, startMark.lon);
    if (!sample) return empty;
    const { speedKt: currentSpd, dirDeg: currentDir } = uvToSpeedDir(
      sample.u,
      sample.v,
    );

    const deltaDirDeg = smallestCircleDelta(routeDir, currentDir);
    const deltaSpeedKt = Math.abs(currentSpd - routeSpd);
    const stale =
      deltaDirDeg > STALE_DIR_DEG || deltaSpeedKt > STALE_SPEED_KT;

    return {
      ready: true,
      stale,
      deltaDirDeg,
      deltaSpeedKt,
      currentDir,
      currentSpd,
      routeDir,
      routeSpd,
    };
  }, [routeMeta, baseWeather, startMark?.lat, startMark?.lon]);
}

// Smallest-circle distance between two compass bearings, in degrees.
// Always in [0, 180]; direction-agnostic.
function smallestCircleDelta(a, b) {
  let d = Math.abs(a - b) % 360;
  if (d > 180) d = 360 - d;
  return d;
}
