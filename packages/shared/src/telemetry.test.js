// telemetry.test.js — contract test for the GPS wire serializer.
//
// Asserts gpsPointToWire produces exactly the canonical wire sample in
// __fixtures__/telemetry_wire_sample.json. The same fixture is validated
// against the backend Pydantic models by
// backend/tests/test_telemetry_wire_contract.py, so this pair pins the
// client→server contract on both sides of the language boundary.
//
// Runs under vitest on Windows/CI (vitest can't run in the cowork
// sandbox — see the project notes).

import { describe, expect, it } from "vitest";

import { gpsPointToWire } from "./telemetry.js";
import fixture from "./__fixtures__/telemetry_wire_sample.json";

// Recorder local-point inputs that should serialize to fixture.gps.
const localPoints = [
  {
    recorded_at: "2026-05-25T15:00:00.000Z",
    lat: 41.935,
    lon: -87.677,
    speed_kts: 5.4,
    heading_deg: 210.5,
    gps_acc_m: 4.0,
  },
  // First fix: GPS hasn't computed velocity/course yet.
  {
    recorded_at: "2026-05-25T15:00:01.000Z",
    lat: 41.9351,
    lon: -87.6771,
    speed_kts: null,
    heading_deg: null,
    gps_acc_m: 6.5,
  },
  // Stationary with the device's "course unavailable" sentinel (-1).
  {
    recorded_at: "2026-05-25T15:00:02.000Z",
    lat: 41.9352,
    lon: -87.6772,
    speed_kts: 0,
    heading_deg: -1,
    gps_acc_m: 8.0,
  },
];

describe("gpsPointToWire", () => {
  it("serializes local points to the canonical wire sample", () => {
    expect(localPoints.map(gpsPointToWire)).toEqual(fixture.gps);
  });

  it("renames fields: recorded_at→t, speed_kts→sog_kts, heading_deg→cog_deg", () => {
    const wire = gpsPointToWire(localPoints[0]);
    expect(wire).toEqual({
      t: "2026-05-25T15:00:00.000Z",
      lat: 41.935,
      lon: -87.677,
      sog_kts: 5.4,
      cog_deg: 210.5,
      gps_acc_m: 4.0,
    });
  });

  it("nulls cog_deg for missing or negative heading", () => {
    expect(gpsPointToWire(localPoints[1]).cog_deg).toBeNull();
    expect(gpsPointToWire(localPoints[2]).cog_deg).toBeNull();
  });

  it("keeps a zero speed (0 kts) rather than nulling it", () => {
    expect(gpsPointToWire(localPoints[2]).sog_kts).toBe(0);
  });
});
