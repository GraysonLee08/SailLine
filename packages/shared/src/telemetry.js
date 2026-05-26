// telemetry.js — wire-format helpers for the /telemetry endpoint.
//
// Single source of truth for the GPS sample shape that both the web
// recorder (frontend/src/hooks/useTrackRecorder.js) and the mobile
// recorder (mobile/src/recorder/useTrackRecorder.ts) POST to
// `POST /api/races/{id}/telemetry`. Keeping this in @sailline/shared
// guarantees both clients serialize identically and gives the backend
// contract test (backend/tests/test_telemetry_wire_contract.py) one
// target to validate against.
//
// The recorder's internal "local point" shape (used by the breadcrumb
// and offline queue) is deliberately kept separate from the wire shape
// so capture/queue logic stays decoupled from the API contract.
//
// Local point shape (produced by each platform's geolocation
// normalizer):
//   { recorded_at, lat, lon, speed_kts, heading_deg, gps_acc_m }
//
// Wire GPS shape (what the backend GpsSample model accepts):
//   { t, lat, lon, sog_kts, cog_deg, gps_acc_m }

/**
 * Translate a recorder local point into the `/telemetry` GPS wire shape.
 *
 * Nulls are emitted (not omitted) for the optional fields so the payload
 * is explicit. `cog_deg` is dropped to null when the heading is missing
 * or negative (devices report -1 / NaN when course isn't computable at
 * low speed); the backend rejects cog values outside [0, 360).
 *
 * @param {{recorded_at: string, lat: number, lon: number,
 *          speed_kts?: number|null, heading_deg?: number|null,
 *          gps_acc_m?: number|null}} point
 * @returns {{t: string, lat: number, lon: number,
 *            sog_kts: number|null, cog_deg: number|null,
 *            gps_acc_m: number|null}}
 */
export function gpsPointToWire(point) {
  return {
    t: point.recorded_at,
    lat: point.lat,
    lon: point.lon,
    sog_kts: Number.isFinite(point.speed_kts) ? point.speed_kts : null,
    cog_deg:
      Number.isFinite(point.heading_deg) && point.heading_deg >= 0
        ? point.heading_deg
        : null,
    gps_acc_m: Number.isFinite(point.gps_acc_m) ? point.gps_acc_m : null,
  };
}
