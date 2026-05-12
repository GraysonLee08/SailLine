"""Server-side attitude estimation via Kalman filter.

Two independent 1D filters fuse accelerometer-derived angles with
gyroscope rate measurements to estimate heel and pitch. Designed to
run server-side: one AttitudeFilter instance per active WS connection,
fed one IMUSample per inbound telemetry frame (~10 Hz).

State per channel: [angle, gyro_bias]
  - Predict: angle += (gyro - bias) * dt
  - Update:  measurement is atan2-derived angle from accel

Sign conventions (NMEA 2000):
  - Heel:  starboard positive, port negative
  - Pitch: bow-up positive, bow-down negative

Phone-to-boat axis mapping is the caller's responsibility (handled in
the WS handler before samples reach this module). For v1 we assume
the per-race calibration step absorbs small mount misalignment.

Parameter tuning notes:
  q_angle, q_bias, r_measure are conservative defaults adapted from
  the well-tested Lauszus implementation. r_measure dominates how much
  the filter trusts the accel vs. the gyro: increase it in heavy
  seas (lateral wave acceleration corrupts gravity reading) and the
  filter will lean more heavily on the gyro between corrections.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass
class IMUSample:
    """One IMU reading in the boat frame.

    Times in seconds (monotonic clock, NOT wall clock — we use it for
    dt only). Accel in m/s^2, rotation rates in rad/s.

    Boat frame: x = starboard, y = forward (bow), z = up.
    """
    t: float
    ax: float
    ay: float
    az: float
    gx: float
    gy: float
    gz: float


@dataclass
class AttitudeChannel:
    """Single-axis Kalman filter for one angle (heel or pitch).

    State: [angle_rad, gyro_bias_rad_per_s]

    Tracking gyro bias is what makes this a 2-state filter rather than
    a 1-state complementary filter — phone gyros have a small constant
    offset that would otherwise integrate into unbounded drift over a
    long race.
    """
    q_angle: float = 0.001     # process noise on angle dynamics (rad^2/s)
    q_bias: float = 0.003      # process noise on bias random walk (rad^2/s^2)
    r_measure: float = 0.03    # measurement noise on accel-derived angle (rad^2)

    # State
    angle: float = 0.0  # rad
    bias: float = 0.0   # rad/s

    # 2x2 error covariance P
    p00: float = 0.0
    p01: float = 0.0
    p10: float = 0.0
    p11: float = 0.0

    def initialize(self, angle_rad: float) -> None:
        """Seed the filter with a starting angle.

        Called once at filter start with the first accel-derived angle
        so the filter doesn't have to converge from zero — cuts cold-
        start time from seconds to one sample, which matters because
        the boat may already be heeled when the WS connects at T-5min.
        """
        self.angle = angle_rad
        self.bias = 0.0
        self.p00 = self.p01 = self.p10 = self.p11 = 0.0

    def update(self, accel_angle_rad: float, rate_rad_s: float, dt: float) -> float:
        """One filter step. Returns the updated angle in radians."""
        # --- Predict ---
        # Subtract estimated bias from the gyro reading, integrate.
        rate_unbiased = rate_rad_s - self.bias
        self.angle += dt * rate_unbiased

        # Predict the covariance update.
        # P = F * P * F^T + Q, expanded for the 2x2 case.
        self.p00 += dt * (dt * self.p11 - self.p01 - self.p10 + self.q_angle)
        self.p01 -= dt * self.p11
        self.p10 -= dt * self.p11
        self.p11 += self.q_bias * dt

        # --- Update ---
        # Innovation covariance (scalar — we measure only the angle).
        s = self.p00 + self.r_measure
        k0 = self.p00 / s
        k1 = self.p10 / s

        # Innovation: how far the accel measurement disagrees with our
        # gyro-propagated estimate.
        y = accel_angle_rad - self.angle
        self.angle += k0 * y
        self.bias += k1 * y

        # Update covariance.
        p00_temp = self.p00
        p01_temp = self.p01
        self.p00 -= k0 * p00_temp
        self.p01 -= k0 * p01_temp
        self.p10 -= k1 * p00_temp
        self.p11 -= k1 * p01_temp

        return self.angle


@dataclass
class AttitudeFilter:
    """Two-channel attitude filter producing heel and pitch in degrees.

    Conventions assumed for axis mapping (caller's responsibility to
    ensure samples are in boat frame):
      heel-from-accel:  atan2(ax, sqrt(... )) — actually atan2(ax, az)
                        works because heel reading is independent of
                        pitch (see derivation in tests).
      pitch-from-accel: atan2(-ay, sqrt(ax^2 + az^2)) — the sqrt term
                        is what tilt-compensates pitch when heel != 0,
                        which matters on a B36.7 routinely heeling 20°.
      heel-rate:        gy (rotation about fore-aft axis)
      pitch-rate:       gx (rotation about transverse axis)
    """
    heel: AttitudeChannel = field(default_factory=AttitudeChannel)
    pitch: AttitudeChannel = field(default_factory=AttitudeChannel)

    _last_t: float | None = None
    _initialized: bool = False

    def reset(self) -> None:
        """Restart the filter — called when a fresh WS connection opens."""
        self.heel = AttitudeChannel()
        self.pitch = AttitudeChannel()
        self._last_t = None
        self._initialized = False

    def step(self, sample: IMUSample) -> tuple[float, float] | None:
        """Process one IMU sample.

        Returns (heel_deg, pitch_deg) or None on the first sample (no
        dt yet) or on a pathological timestamp (zero/negative/huge dt,
        which we skip rather than letting it corrupt the state).
        """
        # Accel-derived angles. Heel is naturally tilt-decoupled;
        # pitch needs the sqrt denominator to stay accurate at heel.
        heel_meas = math.atan2(sample.ax, sample.az)
        pitch_meas = math.atan2(
            -sample.ay,
            math.sqrt(sample.ax * sample.ax + sample.az * sample.az),
        )

        # First sample: initialize from accel, wait for the second
        # sample to actually produce an output (we need a dt).
        if self._last_t is None:
            self.heel.initialize(heel_meas)
            self.pitch.initialize(pitch_meas)
            self._last_t = sample.t
            self._initialized = True
            return None

        dt = sample.t - self._last_t
        self._last_t = sample.t

        # Guard against bad timestamps. A negative dt means clock went
        # backwards; a huge dt means we dropped a long stretch of
        # samples (sleep, network gap). In either case, integrating
        # the gyro over that gap would corrupt the estimate badly.
        if dt <= 0 or dt > 1.0:
            return None

        heel_rad = self.heel.update(heel_meas, sample.gy, dt)
        pitch_rad = self.pitch.update(pitch_meas, sample.gx, dt)

        return math.degrees(heel_rad), math.degrees(pitch_rad)