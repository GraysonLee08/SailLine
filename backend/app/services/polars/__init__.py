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

Beneteau First 36.7 is the only polar at v1. Future classes go in
`BOAT_POLARS` with their own CSV file in this directory.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


# Map of frontend boat_class string → CSV filename in this package.
# Only 36.7 lands at v1; everything else routes to the 36.7 polar with a
# warning at the call site. Add classes here as polars get transcribed.
BOAT_POLARS: dict[str, str] = {
    "Beneteau First 36.7": "beneteau_36_7.csv",
}

DEFAULT_POLAR = "Beneteau First 36.7"


@dataclass(frozen=True)
class Polar:
    """A class polar — TWA rows × TWS cols of boat speed in knots."""
    twa: np.ndarray   # 1D, degrees, ascending in [0, 180]
    tws: np.ndarray   # 1D, knots, ascending
    speed: np.ndarray # 2D, shape (len(twa), len(tws)), knots
    name: str

    def boat_speed(self, twa_deg: float, tws_kts: float) -> float:
        """Bilinear interp boat speed at (TWA, TWS). Clamps to table bounds.

        Returns 0.0 if TWA is below the table's smallest angle (i.e. above
        the close-hauled limit — boat can't sail there). All other
        out-of-range inputs clamp to the nearest table edge.
        """
        # Symmetry fold
        twa = abs(((twa_deg + 180) % 360) - 180)  # 0..180

        # Below the smallest TWA in the table = pinching past close-hauled
        if twa < self.twa[0]:
            return 0.0

        twa_c = float(np.clip(twa, self.twa[0], self.twa[-1]))
        tws_c = float(np.clip(tws_kts, self.tws[0], self.tws[-1]))

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

        return float(
            (1 - fa) * (1 - fs) * v00
            + (1 - fa) * fs * v01
            + fa * (1 - fs) * v10
            + fa * fs * v11
        )


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


__all__ = ["Polar", "load_polar", "load_polar_for_class", "BOAT_POLARS", "DEFAULT_POLAR"]
