// useRegion — derives the active region from (in priority order):
//   1. activeRace marks centroid (overrides; not persisted)
//   2. localStorage "sailline.region" (persisted home region)
//   3. browser GPS (one-shot via useGeolocation)
//   4. IP geolocation (ipapi.co, free tier, no key)
//   5. great_lakes fallback (only if everything else fails)
//
// The user never picks a region — it's inferred. Once detected, the home
// region is persisted so subsequent loads are instant. Race overrides
// don't persist; clearing the race returns to the home region.

import { useEffect, useRef, useState } from "react";
import { useGeolocation } from "./useGeolocation";
import {
  DEFAULT_REGION,
  REGIONS,
  getRegion,
  regionFromMarks,
  regionFromPoint,
} from "../lib/regions";

const STORAGE_KEY = "sailline.region";
const IPAPI_URL = "https://ipapi.co/json/";
const IPAPI_TIMEOUT_MS = 4000;

function readPersisted() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    return v && REGIONS[v] ? v : null;
  } catch {
    return null;
  }
}

function writePersisted(name) {
  try {
    localStorage.setItem(STORAGE_KEY, name);
  } catch {
    /* localStorage disabled */
  }
}

/**
 * Best-effort IP geolocation. Returns {lat, lon} or null on any failure.
 * ipapi.co's free tier is 30k requests/day with no API key. We do one
 * request per page load at most, so this is well within budget.
 */
async function fetchIpLocation(signal) {
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), IPAPI_TIMEOUT_MS);
    // Compose external signal + timeout signal.
    if (signal) {
      signal.addEventListener("abort", () => ctrl.abort(), { once: true });
    }
    const res = await fetch(IPAPI_URL, { signal: ctrl.signal });
    clearTimeout(timer);
    if (!res.ok) return null;
    const data = await res.json();
    const lat = Number(data?.latitude);
    const lon = Number(data?.longitude);
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) return null;
    return { lat, lon };
  } catch {
    return null;
  }
}

/**
 * @param {object|null} activeRace - if provided and has marks, the race's
 *   centroid takes precedence over the user's home region.
 * @returns {object} - the resolved Region object (always defined).
 */
export function useRegion(activeRace) {
  // Home region: persisted across sessions, derived from GPS/IP. Lazy
  // init from localStorage so the first render already has *something*.
  const [homeRegionName, setHomeRegionName] = useState(
    () => readPersisted() || DEFAULT_REGION,
  );

  // Track whether we've ever resolved from a real signal (GPS or IP),
  // so we don't keep re-running detection if the user has already been
  // placed somewhere.
  const detectedRef = useRef(Boolean(readPersisted()));

  const { position } = useGeolocation();

  // GPS-driven region detection. Fires when GPS resolves; updates and
  // persists if it lands in a known region.
  useEffect(() => {
    if (detectedRef.current) return;
    if (!position) return;
    const region = regionFromPoint(position.lat, position.lon);
    if (region) {
      detectedRef.current = true;
      setHomeRegionName(region.name);
      writePersisted(region.name);
    }
  }, [position]);

  // IP geolocation fallback — only runs if (a) we haven't persisted a
  // region and (b) GPS hasn't resolved within ~3s. The geolocation hook
  // is one-shot, so if `position` is still null after that grace period,
  // it likely won't ever arrive (denied / unsupported / timed out).
  useEffect(() => {
    if (detectedRef.current) return;

    const ctrl = new AbortController();
    let cancelled = false;

    const grace = setTimeout(async () => {
      if (cancelled || detectedRef.current) return;
      const loc = await fetchIpLocation(ctrl.signal);
      if (cancelled || detectedRef.current) return;
      if (loc) {
        const region = regionFromPoint(loc.lat, loc.lon);
        if (region) {
          detectedRef.current = true;
          setHomeRegionName(region.name);
          writePersisted(region.name);
        }
      }
      // If neither GPS nor IP geo placed the user in a registered region,
      // we silently leave homeRegionName at DEFAULT_REGION (great_lakes).
    }, 3000);

    return () => {
      cancelled = true;
      clearTimeout(grace);
      ctrl.abort();
    };
  }, []);

  // Race override: if the active race's marks centroid falls in a region,
  // use that. Don't persist — race overrides are transient.
  const raceRegion = activeRace ? regionFromMarks(activeRace.marks) : null;

  return raceRegion || getRegion(homeRegionName);
}
