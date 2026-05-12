"""Tests for the attitude Kalman filter.

The filter has no I/O — all tests are pure-function with synthetic
IMU traces. Each test generates a known motion profile, feeds it
through the filter, and asserts the output recovers the truth
within an acceptable tolerance.

The synthesizer (_synthesize) inverts the filter's measurement model
to generate consistent gravity vectors and gyro rates for a given
attitude profile, so passing tests really do validate the filter
math rather than a self-consistent contrivance.
"""

from __future__ import annotations

import math
import random

import pytest

from app.services.attitude import AttitudeChannel, AttitudeFilter, IMUSample
    

G = 9.81  # m/s^2


def _accel_for_attitude(heel_deg: float, pitch_deg: float) -> tuple[float, float, float]:
    """Compute the gravity vector in the boat frame for given attitude.

    Derivation: apply pitch about x-axis, then heel about y-axis to
    world-up = (0, 0, +1). Result:
        ax = +g * sin(heel) * cos(pitch)
        ay = -g * sin(pitch)
        az = +g * cos(heel) * cos(pitch)

    Inverse formulas the filter uses:
        heel  = atan2(ax, az)
        pitch = atan2(-ay, sqrt(ax^2 + az^2))
    """
    h = math.radians(heel_deg)
    p = math.radians(pitch_deg)
    ax = G * math.sin(h) * math.cos(p)
    ay = -G * math.sin(p)
    az = G * math.cos(h) * math.cos(p)
    return ax, ay, az


def _synthesize(
    heel_profile,
    pitch_profile,
    duration_s: float = 10.0,
    rate_hz: float = 10.0,
    accel_noise_std: float = 0.0,
    gyro_noise_std: float = 0.0,
    gyro_bias: tuple[float, float] = (0.0, 0.0),  # (bias_gx, bias_gy) rad/s
    seed: int = 42,
) -> list[IMUSample]:
    """Generate IMU samples for a given heel/pitch motion profile.

    heel_profile and pitch_profile are functions f(t) -> degrees.
    Gyro rates come from numerical forward-difference of the profile.
    Accel comes from the gravity vector for the instantaneous attitude.
    """
    rng = random.Random(seed)
    dt = 1.0 / rate_hz
    n_steps = int(duration_s * rate_hz)
    bias_gx, bias_gy = gyro_bias

    samples: list[IMUSample] = []
    for i in range(n_steps):
        t = i * dt
        heel = heel_profile(t)
        pitch = pitch_profile(t)

        # Gyro rates from forward difference. Good enough at 10 Hz for
        # the smooth profiles used in these tests.
        gy = math.radians(heel_profile(t + dt) - heel) / dt
        gx = math.radians(pitch_profile(t + dt) - pitch) / dt

        ax, ay, az = _accel_for_attitude(heel, pitch)
        ax += rng.gauss(0, accel_noise_std)
        ay += rng.gauss(0, accel_noise_std)
        az += rng.gauss(0, accel_noise_std)
        gx += rng.gauss(0, gyro_noise_std) + bias_gx
        gy += rng.gauss(0, gyro_noise_std) + bias_gy

        samples.append(IMUSample(
            t=t, ax=ax, ay=ay, az=az, gx=gx, gy=gy, gz=0.0,
        ))
    return samples


def _run(filter_: AttitudeFilter, samples: list[IMUSample]) -> list[tuple[float, float, float]]:
    """Feed samples through the filter, return list of (t, heel_deg, pitch_deg)."""
    out: list[tuple[float, float, float]] = []
    for s in samples:
        result = filter_.step(s)
        if result is not None:
            heel, pitch = result
            out.append((s.t, heel, pitch))
    return out


# ─── Static tilt ──────────────────────────────────────────────────────


class TestStaticTilt:
    """Filter should track a constant attitude, with or without noise."""

    def test_zero_attitude_clean(self):
        f = AttitudeFilter()
        samples = _synthesize(lambda t: 0.0, lambda t: 0.0, duration_s=5.0)
        out = _run(f, samples)
        for _, heel, pitch in out[-10:]:
            assert abs(heel) < 0.5
            assert abs(pitch) < 0.5

    def test_constant_heel_clean(self):
        f = AttitudeFilter()
        samples = _synthesize(lambda t: 20.0, lambda t: 0.0, duration_s=5.0)
        out = _run(f, samples)
        for _, heel, pitch in out[-10:]:
            assert abs(heel - 20.0) < 0.5
            assert abs(pitch) < 0.5

    def test_constant_pitch_clean(self):
        f = AttitudeFilter()
        samples = _synthesize(lambda t: 0.0, lambda t: 5.0, duration_s=5.0)
        out = _run(f, samples)
        for _, heel, pitch in out[-10:]:
            assert abs(heel) < 0.5
            assert abs(pitch - 5.0) < 0.5

    def test_combined_heel_and_pitch_clean(self):
        """At 20° heel + 5° pitch, the tilt-compensated pitch formula
        must give pitch independent of heel — naive atan2(-ay, az)
        would be biased by the cos(heel) factor."""
        f = AttitudeFilter()
        samples = _synthesize(lambda t: 20.0, lambda t: 5.0, duration_s=5.0)
        out = _run(f, samples)
        for _, heel, pitch in out[-10:]:
            assert abs(heel - 20.0) < 0.5
            assert abs(pitch - 5.0) < 0.5

    def test_constant_heel_noisy_accel(self):
        f = AttitudeFilter()
        samples = _synthesize(
            lambda t: 20.0, lambda t: 0.0,
            duration_s=10.0, accel_noise_std=0.5,
        )
        out = _run(f, samples)
        avg_heel = sum(h for _, h, _ in out[-20:]) / 20
        assert abs(avg_heel - 20.0) < 1.0


# ─── Sinusoidal roll (waves) ──────────────────────────────────────────


class TestSinusoidalRoll:
    """Filter should track a sinusoidal heel oscillation through noise —
    the realistic 'boat in waves' scenario."""

    def test_sinusoidal_heel_recovery(self):
        f = AttitudeFilter()
        # 20° amplitude, 4-second period — like a confused chop on Lake MI
        heel_fn = lambda t: 20.0 * math.sin(2 * math.pi * t / 4.0)
        samples = _synthesize(
            heel_fn, lambda t: 0.0,
            duration_s=20.0,
            accel_noise_std=0.5,
            gyro_noise_std=0.01,
        )
        out = _run(f, samples)

        # Skip the first 2 s of warm-up.
        warm = [(t, h) for t, h, _ in out if t > 2.0]
        errors = [h - heel_fn(t) for t, h in warm]
        rms = math.sqrt(sum(e * e for e in errors) / len(errors))
        assert rms < 2.5, f"sinusoidal RMS error {rms:.2f}° too high"


# ─── Gyro bias rejection ──────────────────────────────────────────────


class TestGyroBias:
    """Filter must estimate and subtract gyro bias, or it'll drift
    unboundedly over a 6-hour race."""

    def test_constant_bias_does_not_drift(self):
        f = AttitudeFilter()
        # 1°/s bias on the heel-rate axis (gy)
        bias_rad_s = math.radians(1.0)
        samples = _synthesize(
            lambda t: 0.0, lambda t: 0.0,
            duration_s=60.0,
            gyro_bias=(0.0, bias_rad_s),
        )
        out = _run(f, samples)
        # Without bias estimation, 1°/s for 60 s would drift to 60°.
        # With it, the filter should stay within a few degrees of 0°.
        for _, heel, _ in out[-10:]:
            assert abs(heel) < 5.0


# ─── Step response ────────────────────────────────────────────────────


class TestStepResponse:
    """Filter should follow a sudden attitude change quickly — the gyro
    drives fast response, the accel anchors against drift."""

    def test_step_in_heel(self):
        f = AttitudeFilter()

        def heel(t: float) -> float:
            # Step from 0 to 15° at t=2s, ramped over 0.2 s so the gyro
            # rate stays physical.
            if t < 2.0:
                return 0.0
            if t < 2.2:
                return 15.0 * (t - 2.0) / 0.2
            return 15.0

        samples = _synthesize(heel, lambda t: 0.0, duration_s=5.0)
        out = _run(f, samples)
        # By 1 s after the step, should be within 1.5° of target.
        for _, h, _ in [(t, h, p) for t, h, p in out if t > 3.0]:
            assert abs(h - 15.0) < 1.5


# ─── Initialization & first-sample behavior ───────────────────────────


class TestInitialization:

    def test_first_sample_returns_none(self):
        f = AttitudeFilter()
        s = IMUSample(t=0.0, ax=0.0, ay=0.0, az=G, gx=0.0, gy=0.0, gz=0.0)
        assert f.step(s) is None

    def test_second_sample_emits(self):
        f = AttitudeFilter()
        s1 = IMUSample(t=0.0, ax=0.0, ay=0.0, az=G, gx=0.0, gy=0.0, gz=0.0)
        s2 = IMUSample(t=0.1, ax=0.0, ay=0.0, az=G, gx=0.0, gy=0.0, gz=0.0)
        f.step(s1)
        result = f.step(s2)
        assert result is not None
        heel, pitch = result
        assert abs(heel) < 0.1
        assert abs(pitch) < 0.1

    def test_initialization_at_tilt(self):
        """Filter should start at the tilt of the first sample, not 0 —
        critical because the boat may already be heeled when the WS
        connects at T-5min."""
        f = AttitudeFilter()
        ax, ay, az = _accel_for_attitude(25.0, 0.0)
        s1 = IMUSample(t=0.0, ax=ax, ay=ay, az=az, gx=0.0, gy=0.0, gz=0.0)
        s2 = IMUSample(t=0.1, ax=ax, ay=ay, az=az, gx=0.0, gy=0.0, gz=0.0)
        f.step(s1)
        result = f.step(s2)
        assert result is not None
        heel, _ = result
        assert abs(heel - 25.0) < 1.0


# ─── Robustness to bad inputs ─────────────────────────────────────────


class TestRobustness:
    """Garbage timestamps shouldn't crash or corrupt the filter."""

    def test_zero_dt_skipped(self):
        f = AttitudeFilter()
        s1 = IMUSample(t=1.0, ax=0.0, ay=0.0, az=G, gx=0.0, gy=0.0, gz=0.0)
        s2 = IMUSample(t=1.0, ax=0.0, ay=0.0, az=G, gx=0.0, gy=0.0, gz=0.0)
        f.step(s1)
        assert f.step(s2) is None

    def test_negative_dt_skipped(self):
        f = AttitudeFilter()
        s1 = IMUSample(t=1.0, ax=0.0, ay=0.0, az=G, gx=0.0, gy=0.0, gz=0.0)
        s2 = IMUSample(t=0.5, ax=0.0, ay=0.0, az=G, gx=0.0, gy=0.0, gz=0.0)
        f.step(s1)
        assert f.step(s2) is None

    def test_huge_dt_skipped(self):
        f = AttitudeFilter()
        s1 = IMUSample(t=0.0, ax=0.0, ay=0.0, az=G, gx=0.0, gy=0.0, gz=0.0)
        s2 = IMUSample(t=2.5, ax=0.0, ay=0.0, az=G, gx=0.0, gy=0.0, gz=0.0)
        f.step(s1)
        # A 2.5 s gap shouldn't blow up the state — skip the update.
        assert f.step(s2) is None

    def test_reset_clears_state(self):
        f = AttitudeFilter()
        samples = _synthesize(lambda t: 30.0, lambda t: 0.0, duration_s=3.0)
        _run(f, samples)
        f.reset()
        assert f._last_t is None
        assert f.heel.angle == 0.0
        assert f.pitch.angle == 0.0
        assert f.heel.bias == 0.0
        assert f.pitch.bias == 0.0


# ─── Channel-level direct test ────────────────────────────────────────


class TestAttitudeChannel:
    """Direct unit test of the single-axis filter, independent of axis
    mapping. Useful when debugging the math without the full sample
    machinery."""

    def test_channel_converges_from_init(self):
        ch = AttitudeChannel()
        ch.initialize(math.radians(10.0))
        # Hold steady at 10° with zero gyro for 5 s at 10 Hz.
        for _ in range(50):
            ch.update(math.radians(10.0), 0.0, 0.1)
        assert abs(math.degrees(ch.angle) - 10.0) < 0.2
        # Bias should remain near zero with consistent input.
        assert abs(ch.bias) < math.radians(0.5)