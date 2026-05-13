"""Polar diagrams — boat speed by (TWA, TWS) for class polars.

A "polar" is a 2D table: rows are True Wind Angle (TWA) in degrees, columns
are True Wind Speed (TWS) in knots, cells are boat speed in knots. We store
them as CSV under this package and read them with `load_polar()`.

Symmetry: only TWA in [0, 180] is stored. For TWA in (180, 360], fold to
360 - TWA before lookup — port and starboard are mirror-symmetric for stock
polars (no asymmetric sail plans in v1).

Out-of-range handling: clamps to table bounds. A TWS of 25 kt asks for the
20 kt column; a TWA of 30° asks for the 36° row. This is intentionally
forgiving — the isochrone engine sweeps headings against varying wind, so
edge values matter less than not crashing on them.

`boat_speed()` accepts three optional derating inputs:

* ``hs_m`` — significant wave height (m). When > 0.5 m, applies an upwind
  penalty (max 20% at hs=4.5 m) and small downwind surfing bonus. This is
  the v1 wave model — replace with a per-class wave-derating table once
  WaveWatch III / GLERL ingest is online and we've calibrated against
  Beneteau 36.7 measured data.

* ``density_factor`` — ρ/ρ_std where ρ_std = 1.225 kg/m³. Cold dense air
  in Chicago in November has density_factor ≈ 1.07; hot humid Miami air
  ≈ 0.95. Implemented as effective TWS scaling: effective_tws =
  tws_kts × sqrt(density_factor). Standard density (1.0) ⇒ no change.

* ``margin`` — global polar multiplier in [0, 1]. Default 1.0 = no margin.
  Routers can pass 0.95–0.98 to bake in a conservative buffer for gust
  variability and helmsman performance vs. polar idealization. Cheaper
  than ingesting gust fields and re-running the engine per gust sample.

All three default to no-op so legacy callers (tests, scripts) keep working.

Beneteau First 36.7 is the only polar at v1. Future classes go in
`BOAT_POLARS` with their own CSV file in this directory.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np


# Map of frontend boat_class string → CSV filename in this package.
# Only 36.7 lands at v1; everything else routes to the 36.7 polar with a
# warning at the call site. Add classes here as polars get transcribed.
BOAT_POLARS: dict[str, str] = {
    "Beneteau First 36.7": "beneteau_36_7.csv",
}

DEFAULT_POLAR = "Beneteau First 36.7"


# ─── Wave derating model (v1) ────────────────────────────────────────────
#
# Simple piecewise-linear model good enough for v1. Real boat-class wave
# response curves should replace this once we have per-class measured data.
#
# Calibration intuition for a 36-footer:
#   hs ≤ 0.5 m   → no effect (small chop, well within boat's wavelength)
#   hs = 1.5 m   → ~5% upwind loss (pounding starts)
#   hs = 3.0 m   → ~12% upwind loss (slamming in chop)
#   hs = 4.5 m   → ~20% upwind loss (significantly slowed)
#   downwind     → small gain (surfing) at hs ≥ 1.0 m, capped at +5%
#
# Beam reaches sit in between — interpolated linearly across TWA in [60, 120].

WAVE_PENALTY_THRESHOLD_M = 0.5    # below this, no effect
WAVE_PENALTY_RATE = 0.05          # fraction lost per metre above threshold
WAVE_PENALTY_MAX = 0.20           # cap upwind loss at 20%
WAVE_SURF_BONUS_MAX = 0.05        # cap downwind gain at +5%


def wave_derating(twa_deg: float, hs_m: float) -> float:
    """Return a multiplier on polar boat speed for given wave height.

    Returns 1.0 (no change) when ``hs_m`` is None or below the
    threshold. Linearly interpolates penalty between upwind and downwind
    sectors so beam reach (TWA ~90°) feels half the upwind hit.
    """
    if hs_m is None or hs_m <= WAVE_PENALTY_THRESHOLD_M:
        return 1.0

    # Magnitude of the effect, before sign.
    raw = (hs_m - WAVE_PENALTY_THRESHOLD_M) * WAVE_PENALTY_RATE
    upwind_loss = min(WAVE_PENALTY_MAX, raw)
    downwind_gain = min(WAVE_SURF_BONUS_MAX, raw * 0.25)

    # Fold to [0, 180] symmetric.
    twa = abs(((twa_deg + 180.0) % 360.0) - 180.0)

    # Blend upwind→downwind across [60°, 120°]. Outside this band, pure
    # upwind penalty / downwind bonus.
    if twa <= 60.0:
        return 1.0 - upwind_loss
    if twa >= 120.0:
        return 1.0 + downwind_gain
    # Linear blend across the beam-reach band.
    t = (twa - 60.0) / 60.0
    return (1.0 - upwind_loss) * (1.0 - t) + (1.0 + downwind_gain) * t


@dataclass(frozen=True)
class Polar:
    """A class polar — TWA rows × TWS cols of boat speed in knots."""
    twa: np.ndarray   # 1D, degrees, ascending in [0, 180]
    tws: np.ndarray   # 1D, knots, ascending
    speed: np.ndarray # 2D, shape (len(twa), len(tws)), knots
    name: str

    def boat_speed(
        self,
        twa_deg: float,
        tws_kts: float,
        *,
        hs_m: float = 0.0,
        density_factor: float = 1.0,
        margin: float = 1.0,
    ) -> float:
        """Bilinear interp boat speed at (TWA, TWS), with optional derating.

        Returns 0.0 if TWA is below the table's smallest angle (i.e. above
        the close-hauled limit — boat can't sail there). All other
        out-of-range inputs clamp to the nearest table edge.

        Optional derating:
          hs_m: significant wave height in metres (default 0 = calm).
          density_factor: ρ/ρ_std (default 1 = standard atmosphere).
          margin: global multiplier in [0, 1] (default 1 = no margin).

        With default arguments the behaviour exactly matches the v8
        polar API so existing tests pass unchanged.
        """
        # Symmetry fold
        twa = abs(((twa_deg + 180) % 360) - 180)  # 0..180

        # Below the smallest TWA in the table = pinching past close-hauled
        if twa < self.twa[0]:
            return 0.0

        # Density correction: thicker air at the same wind speed produces
        # more drive force. Scale effective TWS by sqrt(ρ/ρ_std).
        effective_tws_kts = tws_kts * math.sqrt(max(0.0, density_factor))

        twa_c = float(np.clip(twa, self.twa[0], self.twa[-1]))
        tws_c = float(np.clip(effective_tws_kts, self.tws[0], self.tws[-1]))

        # Bracket indices
        i = int(np.searchsorted(self.twa, twa_c, side="right") - 1)
        j = int(np.searchsorted(self.tws, tws_c, side="right") - 1)
        i = min(i, len(self.twa) - 2)
        j = min(j, len(self.tws) - 2)

        a0, a1 = self.twa[i], self.twa[i + 1]
        s0, s1 = self.tws[j], self.tws[j + 1]
        fa = (twa_c - a0) / (a1 - a0) if a1 > a0 else 0.0
        fs = (tws_c - s0) / (s1 - s0) if s1 > s0 else 0.0

        v00 = self.speed[i, j]
        v01 = self.speed[i, j + 1]
        v10 = self.speed[i + 1, j]
        v11 = self.speed[i + 1, j + 1]

        raw = float(
            (1 - fa) * (1 - fs) * v00
            + (1 - fa) * fs * v01
            + fa * (1 - fs) * v10
            + fa * fs * v11
        )

        return raw * wave_derating(twa, hs_m) * margin


def load_polar(path: str | Path) -> Polar:
    """Read a polar CSV into a Polar.

    Format:
      - Lines starting with `#` are comments and skipped.
      - Header row: any text in column 0 (e.g. `twa\\tws`), then TWS values.
      - Data rows: TWA, then boat speed per TWS column.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"polar CSV not found: {p}")

    lines: list[str] = []
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)

    if len(lines) < 2:
        raise ValueError(f"polar CSV has no data rows: {p}")

    header = [c.strip() for c in lines[0].split(",")]
    tws = np.array([float(x) for x in header[1:]], dtype=np.float64)

    twa_vals: list[float] = []
    rows: list[list[float]] = []
    for line in lines[1:]:
        parts = [c.strip() for c in line.split(",")]
        twa_vals.append(float(parts[0]))
        row = [float(x) for x in parts[1:]]
        if len(row) != len(tws):
            raise ValueError(
                f"polar row '{line}' has {len(row)} cells, header has {len(tws)}"
            )
        rows.append(row)

    twa = np.array(twa_vals, dtype=np.float64)
    speed = np.array(rows, dtype=np.float64)

    if not np.all(np.diff(twa) > 0):
        raise ValueError(f"TWA column must be strictly ascending in {p}")
    if not np.all(np.diff(tws) > 0):
        raise ValueError(f"TWS header must be strictly ascending in {p}")

    return Polar(twa=twa, tws=tws, speed=speed, name=p.stem)


def load_polar_for_class(boat_class: str) -> Polar:
    """Resolve a frontend boat_class string to a loaded Polar.

    Falls back to the default polar (36.7) for unknown classes — v1 ships
    one polar and the routing engine still needs to produce something for
    other boats. The caller can warn-log on the fallback path.
    """
    filename = BOAT_POLARS.get(boat_class, BOAT_POLARS[DEFAULT_POLAR])
    return load_polar(Path(__file__).parent / filename)


__all__ = [
    "Polar",
    "load_polar",
    "load_polar_for_class",
    "wave_derating",
    "BOAT_POLARS",
    "DEFAULT_POLAR",
]
