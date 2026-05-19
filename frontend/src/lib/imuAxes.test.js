import { describe, expect, it } from "vitest";

import {
  PHONE_AXES,
  angleDelta,
  applyCalibration,
  createAxisDetector,
  detectPhoneAxis,
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

  it("polarityFlip negates heel and pitch (fore-aft)", () => {
    const out = remapEulerToBoat(
      { alpha: 45, beta: 5, gamma: 12 },
      "fore-aft",
      true,
    );
    expect(out.heel_deg).toBe(-12);
    expect(out.pitch_deg).toBe(-5);
    expect(out.yaw_deg).toBe(45);
  });

  it("polarityFlip negates heel and pitch (port-stbd)", () => {
    const out = remapEulerToBoat(
      { alpha: 90, beta: 12, gamma: 5 },
      "port-stbd",
      true,
    );
    expect(out.heel_deg).toBe(-12);
    expect(out.pitch_deg).toBe(-5);
  });

  it("polarityFlip defaults to false", () => {
    const a = remapEulerToBoat({ alpha: 0, beta: 5, gamma: 10 }, "fore-aft");
    const b = remapEulerToBoat(
      { alpha: 0, beta: 5, gamma: 10 },
      "fore-aft",
      false,
    );
    expect(a).toEqual(b);
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

describe("angleDelta", () => {
  it("returns the smaller of the two arc distances", () => {
    expect(angleDelta(10, 350)).toBe(20);
    expect(angleDelta(350, 10)).toBe(20);
  });
  it("handles wrap-around past 360", () => {
    expect(angleDelta(370, 10)).toBe(0);
    expect(angleDelta(-10, 10)).toBe(20);
  });
  it("returns null when an input is non-finite", () => {
    expect(angleDelta(null, 10)).toBeNull();
    expect(angleDelta(10, Number.NaN)).toBeNull();
  });
});

describe("detectPhoneAxis", () => {
  it("returns null when COG or alpha is missing", () => {
    expect(detectPhoneAxis({ cog: null, alpha: 0 })).toBeNull();
    expect(detectPhoneAxis({ cog: 90, alpha: null })).toBeNull();
    expect(detectPhoneAxis()).toBeNull();
  });

  it("picks fore-aft, normal polarity when alpha matches COG", () => {
    // Boat heading 90° east; phone alpha 90° (top edge points east).
    const r = detectPhoneAxis({ cog: 90, alpha: 88 });
    expect(r.axis).toBe("fore-aft");
    expect(r.polarityFlip).toBe(false);
    expect(r.delta).toBeCloseTo(2, 1);
    expect(r.confidence).toBeGreaterThan(0.9);
  });

  it("picks port-stbd, normal polarity when alpha leads COG by 90°", () => {
    // Boat heading 90°; phone top points south (180°) = COG + 90 wrap.
    const r = detectPhoneAxis({ cog: 90, alpha: 180 });
    expect(r.axis).toBe("port-stbd");
    expect(r.polarityFlip).toBe(false);
    expect(r.delta).toBeCloseTo(0, 1);
  });

  it("picks fore-aft, flipped when alpha is opposite COG", () => {
    // Boat heading 90° east; phone top points west (alpha 270°).
    const r = detectPhoneAxis({ cog: 90, alpha: 270 });
    expect(r.axis).toBe("fore-aft");
    expect(r.polarityFlip).toBe(true);
  });

  it("picks port-stbd, flipped when alpha lags COG by 90° (COG+270)", () => {
    // Boat heading 90°; phone top points north (alpha 0°).
    const r = detectPhoneAxis({ cog: 90, alpha: 0 });
    expect(r.axis).toBe("port-stbd");
    expect(r.polarityFlip).toBe(true);
  });

  it("handles wrap-around (COG near 360°)", () => {
    // Boat heading 350°; phone top within a few degrees of north.
    const r = detectPhoneAxis({ cog: 350, alpha: 5 });
    expect(r.axis).toBe("fore-aft");
    expect(r.polarityFlip).toBe(false);
    expect(r.delta).toBeCloseTo(15, 0);
  });

  it("reports low confidence near the 45° ambiguity boundary", () => {
    // Alpha 45° with COG 0° is halfway between fore-aft and port-stbd.
    const r = detectPhoneAxis({ cog: 0, alpha: 45 });
    expect(r.confidence).toBeLessThan(0.2);
  });
});

describe("createAxisDetector", () => {
  it("returns null until minSamples consistent observations accumulate", () => {
    const det = createAxisDetector({
      minSogKts: 1.5,
      minSamples: 3,
      minConfidence: 0.4,
    });
    expect(det.consider({ cog: 90, alpha: 90, sog_kts: 5 })).toBeNull();
    expect(det.consider({ cog: 92, alpha: 91, sog_kts: 5 })).toBeNull();
    const out = det.consider({ cog: 91, alpha: 90, sog_kts: 5 });
    expect(out).not.toBeNull();
    expect(out.axis).toBe("fore-aft");
    expect(out.polarityFlip).toBe(false);
  });

  it("ignores samples below minSogKts", () => {
    const det = createAxisDetector({ minSamples: 2 });
    expect(det.consider({ cog: 0, alpha: 0, sog_kts: 0.5 })).toBeNull();
    expect(det.consider({ cog: 0, alpha: 0, sog_kts: 0.0 })).toBeNull();
    // Counter is still 0; one quick sample isn't enough either.
    expect(det.consider({ cog: 0, alpha: 0, sog_kts: 3 })).toBeNull();
    // Second moving sample crosses the threshold.
    expect(det.consider({ cog: 0, alpha: 0, sog_kts: 3 })).not.toBeNull();
  });

  it("resets the streak when an inconsistent observation appears", () => {
    const det = createAxisDetector({ minSamples: 3 });
    // Two consistent fore-aft samples — streak at 2.
    det.consider({ cog: 90, alpha: 90, sog_kts: 5 });
    det.consider({ cog: 90, alpha: 90, sog_kts: 5 });
    // Disagreeing sample (would vote port-stbd) breaks the streak —
    // the detector flips its in-flight bet to port-stbd at count 1,
    // not yet locked.
    expect(det.consider({ cog: 90, alpha: 180, sog_kts: 5 })).toBeNull();
    // Two more fore-aft samples after the reset bring fore-aft's
    // streak back up to 2 — still not enough.
    det.consider({ cog: 90, alpha: 90, sog_kts: 5 });
    expect(det.consider({ cog: 90, alpha: 90, sog_kts: 5 })).toBeNull();
    // The third consecutive fore-aft sample finally locks it in.
    const out = det.consider({ cog: 90, alpha: 90, sog_kts: 5 });
    expect(out).not.toBeNull();
    expect(out.axis).toBe("fore-aft");
    expect(out.polarityFlip).toBe(false);
  });

  it("ignores low-confidence samples (45° ambiguity)", () => {
    const det = createAxisDetector({ minSamples: 2, minConfidence: 0.4 });
    expect(det.consider({ cog: 0, alpha: 45, sog_kts: 5 })).toBeNull();
    expect(det.consider({ cog: 0, alpha: 45, sog_kts: 5 })).toBeNull();
    // Neither sample counted; clean fore-aft samples still need 2.
    expect(det.consider({ cog: 0, alpha: 0, sog_kts: 5 })).toBeNull();
    expect(det.consider({ cog: 0, alpha: 0, sog_kts: 5 })).not.toBeNull();
  });

  it("returns the same result on every call after locking", () => {
    const det = createAxisDetector({ minSamples: 2 });
    det.consider({ cog: 0, alpha: 0, sog_kts: 5 });
    const first = det.consider({ cog: 0, alpha: 0, sog_kts: 5 });
    expect(first).not.toBeNull();
    // Even a sample that would have voted port-stbd is ignored once locked.
    const second = det.consider({ cog: 0, alpha: 90, sog_kts: 5 });
    expect(second).toBe(first);
  });

  it("reset() clears the lock", () => {
    const det = createAxisDetector({ minSamples: 2 });
    det.consider({ cog: 0, alpha: 0, sog_kts: 5 });
    det.consider({ cog: 0, alpha: 0, sog_kts: 5 });
    expect(det.result).not.toBeNull();
    det.reset();
    expect(det.result).toBeNull();
    expect(det.consider({ cog: 0, alpha: 90, sog_kts: 5 })).toBeNull();
  });
});
