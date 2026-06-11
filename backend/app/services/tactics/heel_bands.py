"""Per-boat-class optimal-heel bands + trim rule tables.

The tactician's over-heel detector compares sustained heel against the
band for (boat class, TWS, point of sail) and attaches the matching
candidate adjustments to the call. Claude phrases the correction from
this list — it never invents a trim action (spec: "engine decides,
Claude phrases").

Data model
----------
``HEEL_BANDS[boat_class]`` is an ordered list of ``HeelBand`` rows,
matched first-fit on TWS. ``upwind`` bands apply for TWA < 90°,
``downwind`` for TWA >= 90° (downwind heel limits are looser — the
failure mode there is rolling, not rail-down).

Sources: the Beneteau First 36.7 18–22° upwind sweet spot comes from
``Development plan.docx`` §2.1. The reef ladder follows class-assoc
guidance for the 36.7 (full main to ~18 kt, flatten + traveler from
~14 kt). Other classes fall back to ``GENERIC_BANDS`` — conservative
keelboat numbers matching the recap prompt's "10–20° productive,
>25° overpowered" guidance — until per-class data is transcribed
(same sourcing pass as the polar-expansion work).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class HeelBand:
    """One TWS row of a boat's heel table."""

    tws_lo_kt: float
    tws_hi_kt: float
    upwind: bool                 # True = TWA < 90°, False = TWA >= 90°
    band_lo_deg: float           # below this: under-powered (informational)
    band_hi_deg: float           # above this: over-heeled → call
    adjustments: tuple[str, ...] = field(default_factory=tuple)


# Conservative generic-keelboat defaults. Mirrors the post-race recap
# prompt's heel guidance so live calls and the debrief never disagree.
GENERIC_BANDS: list[HeelBand] = [
    HeelBand(0.0, 12.0, True, 5.0, 22.0,
             ("trim on for power", "crew weight to leeward in the lulls")),
    HeelBand(12.0, 18.0, True, 10.0, 24.0,
             ("traveler down", "ease mainsheet", "crew weight to rail")),
    HeelBand(18.0, 99.0, True, 10.0, 25.0,
             ("flatten the main", "ease traveler", "consider a reef")),
    HeelBand(0.0, 99.0, False, 0.0, 30.0,
             ("ease the kite/headsail", "head down in the puffs")),
]


HEEL_BANDS: dict[str, list[HeelBand]] = {
    # Dev plan §2.1: 18–22° upwind sweet spot. Band-hi sits at the top
    # of the sweet spot + small hysteresis so we call sustained
    # over-heel, not the top of a normal puff response.
    "Beneteau First 36.7": [
        HeelBand(0.0, 10.0, True, 8.0, 23.0,
                 ("power up — more twist off", "crew weight inboard")),
        HeelBand(10.0, 14.0, True, 14.0, 24.0,
                 ("traveler down a touch", "crew weight to rail")),
        HeelBand(14.0, 18.0, True, 16.0, 25.0,
                 ("traveler down", "flatten the main", "outhaul on")),
        HeelBand(18.0, 99.0, True, 16.0, 25.0,
                 ("reef the main", "flatten everything", "vang on hard")),
        HeelBand(0.0, 99.0, False, 0.0, 30.0,
                 ("ease the kite", "head down in the puffs",
                  "vang on to stop the roll")),
    ],
}


def band_for(
    boat_class: Optional[str], tws_kt: float, twa_deg: float,
) -> Optional[HeelBand]:
    """First-fit band lookup. Falls back to GENERIC_BANDS for classes
    without a transcribed table. Returns None only if even the generic
    table has no matching row (shouldn't happen — rows span 0–99 kt).
    """
    upwind = (twa_deg % 360.0) < 90.0 or (twa_deg % 360.0) > 270.0
    rows = HEEL_BANDS.get(boat_class or "", GENERIC_BANDS)
    for row in rows:
        if row.upwind == upwind and row.tws_lo_kt <= tws_kt < row.tws_hi_kt:
            return row
    # Fall through to generic if the class table had a gap.
    if rows is not GENERIC_BANDS:
        for row in GENERIC_BANDS:
            if row.upwind == upwind and row.tws_lo_kt <= tws_kt < row.tws_hi_kt:
                return row
    return None
