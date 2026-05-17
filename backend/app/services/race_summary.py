"""Generate the post-race recap + coaching tips via Claude.

Pure I/O wrapper around the Anthropic SDK. Pure-function helpers
(``build_prompt``, ``parse_response``) are exported so tests can
exercise them without needing API credentials.

Shape of the return value (also the shape persisted to
``race_sessions.ai_summary``)::

    {
        "recap":          "string — narrative recap from the model",
        "tips":           ["string", "string", ...],
        "model":          "claude-haiku-4-5-20251001",
        "prompt_version": 1,
        "generated_at":   "2026-05-14T18:30:00Z",
    }

If the Anthropic call fails (no key, network, rate limit, malformed
response), this module returns ``None`` rather than raising. Callers
(the Cloud Run Job, the stats endpoint) treat None as "no summary
yet" and degrade gracefully — the user can hit Regenerate.

Voice
-----
The system prompt asks Claude to play the role of a sailing coach
calibrated to the user's apparent skill level. Race quality drives
length: if the data shows clean execution, the summary stays short.
If the data shows trouble (slow legs, big wind shifts not adapted to,
long stops mid-race), the coach digs deeper. This is enforced via the
system prompt — we don't cap output tokens artificially. See the
PROMPT constant below.

PROMPT_VERSION
--------------
Bump when you change the prompt template, the model, or the
input-shape contract. The Cloud Run Job compares the stored
``prompt_version`` against this constant and regenerates on
mismatch — important when we tune the coach voice or add a field
(e.g. handicap-corrected time in D2).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from app.services.wind_snapshot import summarise_snapshot

log = logging.getLogger(__name__)


# Bump this when you edit ``_SYSTEM_PROMPT`` or change the JSON shape
# the model is asked to produce. Stored summaries with a lower version
# get regenerated automatically.
#
# v3 — heel summary added to the prompt (sourced from imu_samples +
# race_calibrations). When heel data is present the coach can comment
# on heel discipline (depowering, hiking effort, max heel by leg).
PROMPT_VERSION: int = 3

# Default model — the config has the override knob.
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Output token ceiling. Generous enough that the model can give a long
# debrief for a messy race; the system prompt pushes it to stay short
# when the race went cleanly.
_MAX_TOKENS = 1500


# ─── Prompt template ───────────────────────────────────────────────────


_SYSTEM_PROMPT = """\
You are an experienced sailing race coach giving a debrief on a single \
race. Adapt your language to what the data suggests about the \
sailor's skill level — a newer racer who finished slowly with several \
stops needs concepts explained briefly (e.g. "lay line", "header"); a \
fast clean race shows an experienced sailor and you can talk shop. \
Never condescend.

Calibrate length to how much there is to say:
* If the race went smoothly — consistent speed, no long stops, leg \
times reasonable for the wind, heel discipline reasonable — keep the \
recap short (a few sentences) and give 1–2 tips at most.
* If the race had clear problems — slow legs, big wind shifts the \
boat didn't adapt to, long stops, DNF, or sustained over-heeling — \
write a longer recap that walks through what likely happened, and \
give 3–5 specific tips.

Be specific. Reference real numbers (leg times, average speeds, wind \
direction changes, max heel angle, time spent past target heel) rather \
than generic advice. Translate sailor-jargon into plain language the \
first time you use it in a recap (e.g. "lifted (a wind shift that \
lets you point higher)").

If a "Boat heel" block is present in the data, use it to coach on \
heel discipline. Rough guidance on heel for most racing keelboats:
* 10–20° is the productive range upwind.
* Sustained heel past ~25° usually means the rig is overpowered — \
  flatten with twist, traveler, or vang ease; reef in stronger air.
* If max heel is mild but average heel is high, hiking effort is \
  probably the lever; if max heel spikes but average is fine, those \
  are likely puffs the trim missed.
Frame heel comments alongside the leg they belong to where possible. \
If heel data is missing or sparse, do not invent it.

The phone might be mounted in a position that doesn't perfectly \
represent boat axes, so treat any single sample with mild skepticism — \
sustained trends matter more than peaks.

Output STRICT JSON of exactly this shape — no markdown, no code fences:

{
  "recap": "string — the narrative recap, can include line breaks",
  "tips":  ["string", "string", ...]
}

Do not include any keys other than recap and tips. Do not echo the \
input data. Do not apologise for anything.\
"""


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    m = int(seconds // 60)
    s = int(seconds - m * 60)
    if m < 60:
        return f"{m}:{s:02d}"
    h = m // 60
    m = m % 60
    return f"{h}h {m:02d}:{s:02d}"


def _fmt_distance_nm(meters: float) -> str:
    nm = meters / 1852.0
    if nm < 1:
        return f"{meters:.0f} m ({nm:.2f} nm)"
    return f"{nm:.2f} nm"


def _fmt_dir_deg(deg: float) -> str:
    pts = [
        "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
    ]
    idx = int((deg / 22.5) + 0.5) % 16
    return f"{deg:.0f}° ({pts[idx]})"


def build_prompt(
    *,
    race_name: Optional[str],
    boat_class: Optional[str],
    stats: dict,
    wind_snapshot: Optional[dict] = None,
    heel_summary: Optional[dict] = None,
) -> str:
    """Render a deterministic user-message payload from race data.

    Pure function. ``stats`` is the dict returned by
    ``race_stats.RaceStats.to_dict()``. ``wind_snapshot`` is the dict
    persisted on the race row, or None when wind data wasn't
    available. ``heel_summary`` is the dict returned by
    ``heel_stats.compute_heel_summary``, or None when no IMU data is
    available for the race. The Cloud Run Job assembles these and
    passes them in.
    """
    name = race_name or "Untitled race"
    boat = boat_class or "unspecified class"
    elapsed = _fmt_duration(stats.get("elapsed_s", 0.0))
    distance = _fmt_distance_nm(stats.get("distance_m", 0.0))
    avg_kt = stats.get("avg_sog_kt", 0.0)
    max_kt = stats.get("max_sog_kt", 0.0)
    mov_kt = stats.get("avg_moving_sog_kt", 0.0)
    stopped_s = stats.get("stopped_s", 0.0)
    corrected_s = stats.get("corrected_time_s")
    corrected_using = stats.get("corrected_using")
    rating = stats.get("rating_seconds_per_mile")

    lines = [
        f"Race: {name}",
        f"Boat class: {boat}",
        f"Distance sailed: {distance}",
        f"Elapsed time: {elapsed}",
        f"Average SOG: {avg_kt:.1f} kt (moving-only: {mov_kt:.1f} kt, "
        f"max: {max_kt:.1f} kt)",
        f"Time stopped (<0.5 kt): {_fmt_duration(stopped_s)}",
    ]
    if corrected_s is not None and rating is not None and corrected_using:
        label_map = {
            "hcp":    "ToD HCP (buoy, spinnaker)",
            "dhcp":   "ToD DHCP (random leg, spinnaker)",
            "nshcp":  "ToD NSHCP (buoy, non-spinnaker)",
            "dnshcp": "ToD DNSHCP (random leg, non-spinnaker)",
        }
        lines.append(
            f"Corrected time: {_fmt_duration(corrected_s)} "
            f"(rating {rating} s/nm, {label_map.get(corrected_using, corrected_using)})"
        )
    lines.append("")

    legs = stats.get("legs") or []
    if legs:
        lines.append("Leg-by-leg splits:")
        for leg in legs:
            lines.append(
                f"  Leg {leg['leg_index'] + 1}: "
                f"{leg['from_label']} → {leg['to_label']}, "
                f"{_fmt_distance_nm(leg['distance_m'])} in "
                f"{_fmt_duration(leg['elapsed_s'])}, "
                f"avg {leg['avg_sog_kt']:.1f} kt"
            )
        if len(legs) < (len(stats.get("legs", [])) or 0):
            # placeholder for future "DNF after leg N" annotation
            pass
        lines.append("")
    else:
        lines.append("Leg splits: none (no marks rounded — DNF or no course).")
        lines.append("")

    if wind_snapshot:
        wsum = summarise_snapshot(wind_snapshot)
        if wsum.get("mean_speed_kt") is not None:
            lines.append("Wind during the race (from forecast snapshot):")
            lines.append(
                f"  Average: {wsum['mean_speed_kt']:.1f} kt from "
                f"{_fmt_dir_deg(wsum['mean_dir_deg'])}"
            )
            lines.append(f"  Max gust in forecast: {wsum['max_speed_kt']:.1f} kt")
            lines.append(
                f"  Direction range across race window: "
                f"{wsum['dir_range_deg']:.0f}° "
                f"(a value >20° means the wind shifted noticeably)"
            )
            if wsum.get("cell_coverage", 1.0) < 0.5:
                lines.append(
                    "  Note: forecast covered <50% of the race area; "
                    "wind context is approximate."
                )
            lines.append("")
        else:
            lines.append(
                "Wind data: no forecast coverage at this location/time."
            )
            lines.append("")
    else:
        lines.append("Wind data: not available for this race.")
        lines.append("")

    if heel_summary and heel_summary.get("sample_count", 0) > 0:
        max_abs = heel_summary.get("max_heel_abs_deg", 0.0)
        max_signed = heel_summary.get("max_heel_deg", 0.0)
        avg_abs = heel_summary.get("avg_heel_abs_deg", 0.0)
        pct10 = heel_summary.get("pct_time_heeled_gt_10", 0.0)
        pct20 = heel_summary.get("pct_time_heeled_gt_20", 0.0)
        max_pitch = heel_summary.get("max_pitch_abs_deg", 0.0)
        side = "starboard" if max_signed >= 0 else "port"
        lines.append("Boat heel (from phone orientation sensor):")
        lines.append(
            f"  Max heel: {max_abs:.0f}° to {side}; "
            f"average |heel|: {avg_abs:.1f}°"
        )
        lines.append(
            f"  Time past 10°: {pct10*100:.0f}% of race; "
            f"past 20°: {pct20*100:.0f}%"
        )
        lines.append(f"  Max pitch: {max_pitch:.0f}°")
        legs = heel_summary.get("by_leg") or []
        if legs:
            lines.append("  Per-leg heel:")
            for leg in legs:
                lines.append(
                    f"    Leg {leg['leg_index'] + 1}: "
                    f"max {leg['max_heel_abs_deg']:.0f}°, "
                    f"avg {leg['avg_heel_abs_deg']:.1f}° "
                    f"({leg['sample_count']} samples)"
                )
        lines.append(
            "  Note: phone-on-table mount; absolute axis may be approximate "
            "but sustained trends are reliable."
        )
        lines.append("")

    lines.append(
        "Give a debrief in the JSON shape described in the system prompt."
    )
    return "\n".join(lines)


# ─── Response parsing ─────────────────────────────────────────────────


def parse_response(raw_text: str) -> Optional[dict]:
    """Pull ``{recap, tips}`` out of the model's reply.

    The system prompt insists on strict JSON, but models sometimes
    wrap it in a code fence or prepend a sentence. This parser is
    forgiving: it finds the first ``{`` and the matching ``}``, then
    json-loads that slice.

    Returns None if the response can't be parsed into the expected
    shape — caller treats that as "summary unavailable" and falls
    back to the stats-only view.
    """
    if not raw_text:
        return None
    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start < 0 or end <= start:
        log.warning("ai summary: no JSON object found in response")
        return None
    try:
        obj = json.loads(raw_text[start : end + 1])
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("ai summary: failed to parse JSON (%s)", e)
        return None
    if not isinstance(obj, dict):
        return None
    recap = obj.get("recap")
    tips = obj.get("tips") or []
    if not isinstance(recap, str) or not isinstance(tips, list):
        log.warning("ai summary: response shape unexpected: %r", obj)
        return None
    # Coerce tips to strings; drop non-strings rather than fail.
    tips_str = [t for t in tips if isinstance(t, str)]
    return {"recap": recap.strip(), "tips": tips_str}


# ─── Anthropic SDK bridge ─────────────────────────────────────────────


def _build_client(api_key: str) -> Any:
    """Lazy SDK import — keeps app startup fast and means tests don't
    need the sdk installed if they only exercise the pure helpers."""
    from anthropic import Anthropic  # type: ignore[import-not-found]
    return Anthropic(api_key=api_key)


def generate_summary(
    *,
    race_name: Optional[str],
    boat_class: Optional[str],
    stats: dict,
    wind_snapshot: Optional[dict] = None,
    heel_summary: Optional[dict] = None,
    client: Any = None,
    model: Optional[str] = None,
) -> Optional[dict]:
    """Call Claude and return the parsed summary dict. Returns None on
    any failure — callers never see exceptions from this layer.

    ``client`` is injectable for tests; production callers pass None
    and let us construct the real SDK client from settings.
    """
    # Lazy import — keeps this module importable even when app.config
    # can't construct (no env vars, dev environments, the OneDrive-
    # mounted sandbox). Settings is only needed when we actually try
    # to call the API.
    if client is None:
        try:
            from app.config import get_settings
            settings = get_settings()
            mdl = model or settings.anthropic_model or _DEFAULT_MODEL
            key = settings.anthropic_api_key
        except Exception as e:  # noqa: BLE001
            log.warning("ai summary: settings unavailable (%s)", e)
            return None
        if not key:
            log.info("ai summary: ANTHROPIC_API_KEY not set, skipping")
            return None
        try:
            client = _build_client(key)
        except Exception as e:  # noqa: BLE001 - SDK import-time issues
            log.warning("ai summary: failed to build Anthropic client (%s)", e)
            return None
    else:
        mdl = model or _DEFAULT_MODEL

    prompt = build_prompt(
        race_name=race_name,
        boat_class=boat_class,
        stats=stats,
        wind_snapshot=wind_snapshot,
        heel_summary=heel_summary,
    )

    try:
        msg = client.messages.create(
            model=mdl,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as e:  # noqa: BLE001 - keep this safe; network/auth/rate-limit
        log.warning("ai summary: Anthropic call failed (%s)", e)
        return None

    raw_text = _extract_text(msg)
    parsed = parse_response(raw_text)
    if parsed is None:
        return None

    return {
        **parsed,
        "model": mdl,
        "prompt_version": PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _extract_text(msg: Any) -> str:
    """Pull the text out of an Anthropic Message response.

    The SDK shape is ``Message.content -> list[ContentBlock]``. We
    concatenate every ``text`` block, in order. Defensive against
    older/newer SDK shapes so a minor SDK bump doesn't take stats
    down.
    """
    try:
        blocks = getattr(msg, "content", None) or []
        out = []
        for b in blocks:
            t = getattr(b, "text", None)
            if isinstance(t, str):
                out.append(t)
            elif isinstance(b, dict) and isinstance(b.get("text"), str):
                out.append(b["text"])
        return "".join(out)
    except Exception:  # noqa: BLE001
        return ""
