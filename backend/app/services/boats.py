"""Boat class registry — physical specs the routing engine needs.

Centralizes per-class data so polars, drafts, and (eventually) hull-speed
caps live in one place. Currently consumed by:

  - ``app.services.polars`` for polar CSV lookup
  - ``app.routers.routing`` for draft → minimum-depth derivation

When a class is missing here we fall back to ``GENERIC`` — same pattern as
the existing polar fallback. Keeps the API resilient to frontend boat-class
strings drifting ahead of backend support.

Drafts are sourced from manufacturer specs and converted to meters. They
represent the design draft (centerboard down for swing-keel boats; not
currently used at v1 but worth flagging for boats like the J/124).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BoatSpec:
    """Physical attributes of a class polar."""
    name: str
    polar_csv: str
    draft_m: float          # design draft, meters
    loa_m: float            # length overall, meters (informational)
    displacement_kg: float  # rigged displacement (informational)


# Source: manufacturer pages and ORC certificates. Cross-reference with
# the customer's own boat doc before betting your hull on it.
GENERIC = BoatSpec(
    name="Generic PHRF/ORC",
    polar_csv="beneteau_36_7.csv",  # 36.7 polar is the v1 fallback
    draft_m=1.83,                    # 6 ft, conservative mid-fleet default
    loa_m=11.0,
    displacement_kg=5500.0,
)


BOATS: dict[str, BoatSpec] = {
    "Beneteau First 36.7": BoatSpec(
        name="Beneteau First 36.7",
        polar_csv="beneteau_36_7.csv",
        draft_m=2.10,                # 6 ft 11 in
        loa_m=11.07,
        displacement_kg=5550.0,
    ),
    "J/105": BoatSpec(
        name="J/105",
        polar_csv="beneteau_36_7.csv",   # placeholder; fall back until polar transcribed
        draft_m=1.98,                    # 6 ft 6 in
        loa_m=10.52,
        displacement_kg=3674.0,
    ),
    "J/109": BoatSpec(
        name="J/109",
        polar_csv="beneteau_36_7.csv",
        draft_m=2.16,                    # 7 ft 1 in
        loa_m=10.76,
        displacement_kg=4990.0,
    ),
    "J/111": BoatSpec(
        name="J/111",
        polar_csv="beneteau_36_7.csv",
        draft_m=2.36,                    # 7 ft 9 in
        loa_m=11.05,
        displacement_kg=4990.0,
    ),
    "Farr 40": BoatSpec(
        name="Farr 40",
        polar_csv="beneteau_36_7.csv",
        draft_m=2.64,                    # 8 ft 8 in
        loa_m=12.20,
        displacement_kg=5780.0,
    ),
    "Beneteau First 40.7": BoatSpec(
        name="Beneteau First 40.7",
        polar_csv="beneteau_36_7.csv",
        draft_m=2.30,                    # 7 ft 6 in
        loa_m=12.42,
        displacement_kg=7280.0,
    ),
    "Tartan 10": BoatSpec(
        name="Tartan 10",
        polar_csv="beneteau_36_7.csv",
        draft_m=1.75,                    # 5 ft 9 in
        loa_m=10.06,
        displacement_kg=2954.0,
    ),
    "Generic PHRF/ORC": GENERIC,
}


def spec_for_class(boat_class: str) -> BoatSpec:
    """Return the BoatSpec for a class, or GENERIC if unknown."""
    return BOATS.get(boat_class, GENERIC)


__all__ = ["BoatSpec", "BOATS", "GENERIC", "spec_for_class"]
