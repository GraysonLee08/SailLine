"""Standalone isochrone CLI — runs the engine offline against a saved wind grid.

Usage from backend/:

    # Fetch a fresh wind grid (gunzip strips the Content-Encoding):
    curl -s "https://sailline.web.app/api/weather?region=conus&source=hrrr" \\
        --compressed > scripts/output/wind.json

    python -m scripts.isochrone \\
        --start 42.3636,-87.8261 \\
        --finish 41.8881,-87.6132 \\
        --polar app/services/polars/beneteau_36_7.csv \\
        --wind scripts/output/wind.json \\
        --output scripts/output/route.geojson

The Waukegan harbor entrance and Chicago harbor entrance coordinates above
are the defaults — invoke with no --start/--finish to run the May 9
delivery test scenario directly.

Sanity-check output:
    - "reached: True" — finish radius hit
    - tack_count > 0 if forecast is southerly (boat must beat upwind)
    - total_minutes plausible vs. straight-line distance / 5.5 kt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make `app.*` importable when running from backend/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.polars import load_polar  # noqa: E402
from app.services.routing.isochrone import (  # noqa: E402
    WindField,
    compute_isochrone_route,
    haversine_m,
    M_PER_NM,
    route_to_geojson,
)


# Defaults: Waukegan harbor entrance → Chicago harbor entrance.
DEFAULT_START = (42.3636, -87.8261)
DEFAULT_FINISH = (41.8881, -87.6132)


def _parse_latlon(s: str) -> tuple[float, float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"expected 'lat,lon', got {s!r}")
    return float(parts[0]), float(parts[1])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute an isochrone route from a saved wind grid.",
    )
    parser.add_argument(
        "--start", type=_parse_latlon, default=DEFAULT_START,
        help="start lat,lon (default: Waukegan harbor)",
    )
    parser.add_argument(
        "--finish", type=_parse_latlon, default=DEFAULT_FINISH,
        help="finish lat,lon (default: Chicago harbor)",
    )
    parser.add_argument(
        "--polar", type=Path,
        default=Path(__file__).resolve().parents[1] / "app/services/polars/beneteau_36_7.csv",
        help="path to polar CSV",
    )
    parser.add_argument(
        "--wind", type=Path, required=True,
        help="path to wind JSON (fetch from /api/weather?region=conus&source=hrrr --compressed)",
    )
    parser.add_argument(
        "--output", type=Path,
        default=Path(__file__).resolve().parent / "output/route.geojson",
        help="output GeoJSON path",
    )
    parser.add_argument("--dt-min", type=float, default=5.0, help="time step in minutes")
    parser.add_argument(
        "--heading-step", type=float, default=5.0,
        help="heading sweep step in degrees",
    )
    parser.add_argument("--max-iterations", type=int, default=240)
    parser.add_argument("--finish-radius-nm", type=float, default=0.5)
    args = parser.parse_args()

    polar = load_polar(args.polar)
    print(f"polar: {polar.name} — TWA {polar.twa[0]}..{polar.twa[-1]}, TWS {polar.tws[0]}..{polar.tws[-1]}")

    payload = json.loads(args.wind.read_text(encoding="utf-8"))
    wind = WindField.from_payload(payload)
    print(
        f"wind:  {wind.source} valid={wind.valid_time} "
        f"grid={len(wind.lats)}x{len(wind.lons)} "
        f"lat[{wind.lats[0]:.2f}..{wind.lats[-1]:.2f}] lon[{wind.lons[0]:.2f}..{wind.lons[-1]:.2f}]"
    )

    # Sanity-check the start position is inside the wind grid
    if not wind.contains(*args.start):
        print(f"ERROR: start {args.start} is outside the wind grid", file=sys.stderr)
        return 2
    if not wind.contains(*args.finish):
        print(f"ERROR: finish {args.finish} is outside the wind grid", file=sys.stderr)
        return 2

    # Sample wind at start so the user can see what we're routing in
    uv = wind.sample(*args.start)
    if uv:
        from app.services.routing.isochrone import uv_to_tws_twd
        tws, twd = uv_to_tws_twd(*uv)
        print(f"wind at start: {tws:.1f} kt @ {twd:.0f}° (FROM)")

    rhumb_nm = haversine_m(*args.start, *args.finish) / M_PER_NM
    print(f"rhumb: {rhumb_nm:.1f} nm")

    print("running isochrone…")
    result = compute_isochrone_route(
        start=args.start,
        finish=args.finish,
        polar=polar,
        wind=wind,
        dt_minutes=args.dt_min,
        heading_step_deg=args.heading_step,
        max_iterations=args.max_iterations,
        finish_radius_nm=args.finish_radius_nm,
    )

    print(
        f"  reached={result.reached}  iterations={result.iterations}  "
        f"nodes={result.nodes_explored}  tacks={result.tack_count}  "
        f"time={result.total_minutes:.1f} min ({result.total_minutes/60:.2f} h)"
    )
    if result.total_minutes > 0:
        avg_kts = (rhumb_nm / (result.total_minutes / 60.0))
        print(f"  rhumb-avg implied speed: {avg_kts:.2f} kt")

    feature = route_to_geojson(result, properties={
        "start": list(args.start),
        "finish": list(args.finish),
        "polar": polar.name,
        "wind_valid_time": wind.valid_time,
    })
    fc = {"type": "FeatureCollection", "features": [feature]}

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fc, indent=2), encoding="utf-8")
    print(f"wrote {args.output}")

    if not result.reached:
        print("WARNING: did not reach finish — closest-approach path returned", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
