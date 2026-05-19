// usePermissionStatus.js — React hook around lib/permissionStatus.
//
// Subscribes to the platform Location-permission probe and re-renders
// the consumer when state changes. Returns the *raw* Status object;
// callers should pass it through `classifyStatus` when deciding whether
// to render a banner, so the classification rule stays in one place.
//
// The subscription is created exactly once per hook instance and torn
// down on unmount; the underlying lib handles the platform branching
// (PermissionStatus.onchange on web, visibilitychange + safety poll on
// native).

import { useEffect, useState } from "react";

import { subscribeLocationPermission } from "../lib/permissionStatus";

/**
 * @returns {{state, background, source} | null} status, or null while
 *          the initial async snapshot is still in flight.
 */
export function usePermissionStatus() {
  const [status, setStatus] = useState(null);

  useEffect(() => {
    const unsubscribe = subscribeLocationPermission((snap) => {
      setStatus(snap);
    });
    return unsubscribe;
  }, []);

  return status;
}
