// @sailline/shared — barrel for the framework-agnostic client modules.
//
// These modules contain no DOM/React/browser or Mapbox dependencies and
// are consumed by both the web app (Vite) and the mobile app (Expo/Metro).
// Browser- or UI-coupled helpers (geolocation, permissionStatus, motion)
// intentionally remain in frontend/src/lib and are NOT re-exported here.
//
// NOTE (2026-05-24): the web app has not yet been cut over to import from
// this package — that is a supervised step (see the morning runbook).
// Until then these are copies of frontend/src/lib/*; treat this package as
// the source of truth going forward.

export * from "./latlon.js";
export * from "./regions.js";
export * from "./boatClasses.js";
export * from "./morfMarks.js";
export * from "./morfCourses.js";
export * from "./markRounding.js";
export * from "./windBarb.js";
export * from "./imuAxes.js";
