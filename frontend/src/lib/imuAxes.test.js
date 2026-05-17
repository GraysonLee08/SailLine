import { describe, expect, it } from "vitest";

import {
  PHONE_AXES,
  applyCalibration,
  remapEulerToBoat,
  wrap360,
} from "./imuAxes";

describe("PHONE_AXES", () => {
  it("exposes both supported axes", () => {
    expect(PHONE_AXES).toEqual(["fore-aft", "port-stbd"]);
  });
});

describe("wrap360", () => {
  it("wraps negative angles", () => {
    expect(wrap360(-10)).toBe(350);
  });
  it("wraps overflow", () => {
    expect(wrap360(370)).toBe(10);
  });
  it("returns null on non-finite", () => {
    expect(wrap360(null)).toBeNull();
    expect(wrap360(undefined)).toBeNull();
    expect(wrap360(Number.NaN)).toBeNull();
  });
});

describe("remapEulerToBoat", () => {
  it("returns null when inputs are missing", () => {
    expect(remapEulerToBoat(null)).toBeNull();
    expect(remapEulerToBoat({ alpha: 0, beta: null, gamma: 0 })).toBeNull();
  });

  it("fore-aft: gamma→heel, beta→pitch", () => {
    const out = remapEulerToBoat(
      { alpha: 45, beta: 5, gamma: 12 },
      "fore-aft",
    );
    expect(out).toMatchObject({
      heel_deg: 12,
      pitch_deg: 5,
      yaw_deg: 45,
    });
  });

  it("port-stbd: beta→heel, gamma→pitch", () => {
    const out = remapEulerToBoat(
      { alpha: 90, beta: 12, gamma: 5 },
      "port-stbd",
    );
    expect(out).toMatchObject({
      heel_deg: 12,
      pitch_deg: 5,
      yaw_deg: 90,
    });
  });

  it("clamps heel/pitch into [-90, 90]", () => {
    const out = remapEulerToBoat(
      { alpha: 0, beta: 120, gamma: -150 },
      "fore-aft",
    );
    expect(out.heel_deg).toBe(-90);
    expect(out.pitch_deg).toBe(90);
  });

  it("normalises yaw via wrap360", () => {
    const out = remapEulerToBoat(
      { alpha: 370, beta: 0, gamma: 0 },
      "fore-aft",
    );
    expect(out.yaw_deg).toBe(10);
  });

  it("passes through null yaw when alpha missing", () => {
    const out = remapEulerToBoat(
      { alpha: null, beta: 5, gamma: 10 },
      "fore-aft",
    );
    expect(out).not.toBeNull();
    expect(out.yaw_deg).toBeNull();
  });

  it("defaults to fore-aft when axis omitted", () => {
    const out = remapEulerToBoat({ alpha: 0, beta: 5, gamma: 10 });
    expect(out.heel_deg).toBe(10); // gamma
    expect(out.pitch_deg).toBe(5); // beta
  });
});

describe("applyCalibration", () => {
  it("subtracts heel and pitch offsets", () => {
    const corrected = applyCalibration(
      { heel_deg: 15, pitch_deg: 3, yaw_deg: 90 },
      { heel_zero_offset_deg: 5, pitch_zero_offset_deg: -2 },
    );
    expect(corrected).toMatchObject({
      heel_deg: 10,
      pitch_deg: 5,
      yaw_deg: 90,
    });
  });

  it("returns input untouched when offsets null", () => {
    const corrected = applyCalibration(
      { heel_deg: 7, pitch_deg: 1, yaw_deg: 180 },
      null,
    );
    expect(corrected).toMatchObject({
      heel_deg: 7,
      pitch_deg: 1,
      yaw_deg: 180,
    });
  });

  it("does not touch yaw", () => {
    const corrected = applyCalibration(
      { heel_deg: 5, pitch_deg: 5, yaw_deg: 200 },
      { heel_zero_offset_deg: 1, pitch_zero_offset_deg: 1 },
    );
    expect(corrected.yaw_deg).toBe(200);
  });

  it("returns null when reading is null", () => {
    expect(applyCalibration(null, { heel_zero_offset_deg: 1 })).toBeNull();
  });
});
