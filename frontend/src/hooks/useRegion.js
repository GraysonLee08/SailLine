// useRegion — derives the user's BASE region (conus or hawaii) from:
//   1. activeRace marks centroid — overrides; not persisted
//   2. localStorage "sailline.region" — last detected, persisted
//   3. browser GPS — one-shot via useGeolocation
//   4. IP geolocation — ipapi.co, free, no key
//   5. DEFAULT_BASE_REGION (conus) — fallback if everything fails
//
// The user never picks a region. Once detected, the home base is persisted
// so subsequent loads are instant. Race overrides don't persist; clearing
// the race returns to the home base.
//
// VENUES are NOT handled here — they're driven by viewport position and
// zoom level, which only MapView knows. MapView calls venueForPoint() on
// each moveend to figure out whether to load a venue overlay. That
// separation keeps useRegion's concern simple ("what's the user's base
// coverage?") and avoids circular dependencies between hook and map.

import { useEffect, useRef, useState } from "react";
import { useGeolocation } from "./useGeolocation";
import {
  DEFAULT_BASE_REGION,
  REGIONS,
  baseRegionForPoint,
  getRegion,
  marksCentroid,
} from "../lib/regions";

const STORAGE_KEY = "sailline.region";
const IPAPI_URL = "https://ipapi.co/json/";
const IPAPI_TIMEOUT_MS = 4000;
const GPS_GRACE_MS = 3000;

function readPersisted() {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    // Validate against the registry AND require kind="base" — older
    // builds wrote venue names here; ignore those silently.
    return v && REGIONS[v]?.kind === "base" ? v : null;
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

async function fetchIpLocation(signal) {
  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), IPAPI_TIMEOUT_MS);
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
 *   centroid takes precedence over the user's home base. Useful when a
 *   user in CONUS loads a Hawaii race or vice versa.
 * @returns {object} the resolved BASE Region object (always defined).
 */
export function useRegion(activeRace) {
  // Lazy init from localStorage so the first render already has something
  // sensible — avoids a flash from CONUS to (e.g.) Hawaii on second load
  // for users who've already been detected.
  const [homeRegionName, setHomeRegionName] = useState(
    () => readPersisted() || DEFAULT_BASE_REGION,
  );

  const detectedRef = useRef(Boolean(readPersisted()));
  const { position } = useGeolocation();

  // GPS detection.
  useEffect(() => {
    if (detectedRef.current) return;
    if (!position) return;
    const region = baseRegionForPoint(position.lat, position.lon);
    if (region) {
      detectedRef.current = true;
      setHomeRegionName(region.name);
      writePersisted(region.name);
    }
  }, [position]);

  // IP geolocation fallback after a grace period in case GPS is denied/slow.
  useEffect(() => {
    if (detectedRef.current) return;

    const ctrl = new AbortController();
    let cancelled = false;

    const grace = setTimeout(async () => {
      if (cancelled || detectedRef.current) return;
      const loc = await fetchIpLocation(ctrl.signal);
      if (cancelled || detectedRef.current) return;
      if (loc) {
        const region = baseRegionForPoint(loc.lat, loc.lon);
        if (region) {
          detectedRef.current = true;
          setHomeRegionName(region.name);
          writePersisted(region.name);
        }
      }
      // Outside any base region (international user) — leave
      // homeRegionName at DEFAULT_BASE_REGION so the user at least sees
      // the map.
    }, GPS_GRACE_MS);

    return () => {
      cancelled = true;
      clearTimeout(grace);
      ctrl.abort();
    };
  }, []);

  // Race override. Doesn't persist — only affects the current view while
  // a race is active. Falls back to the home base if the race is in an
  // unrecognized region (e.g. a user-defined race in international waters).
  if (activeRace) {
    const c = marksCentroid(activeRace.marks);
    if (c) {
      const raceBase = baseRegionForPoint(c.lat, c.lon);
      if (raceBase) return raceBase;
    }
  }

  return getRegion(homeRegionName);
}
