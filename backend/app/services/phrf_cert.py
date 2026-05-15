"""Parse a 2026 MWPHRF Handicap Certificate PDF into structured fields.

Pure function: bytes in, dataclass out. No I/O, no GCS.

The MWPHRF cert template (the "Gaucho" sample we have on file) lays
the cert out in a predictable shape: a single page with labeled fields
in three column-pairs, then four labeled handicap lines. ``pypdf`` is
enough to extract the text — pdfplumber would buy us better column
detection but the labels are unique enough that line-by-line regex
holds up.

Returns a ``ParsedCert`` with every recognised field. Any field we
can't find comes back as None — the caller (BoatEditor on the
frontend, after a round trip through ``POST /api/boats/{id}/cert``)
presents the parsed fields with what's missing left blank, so the user
can fix it manually.

If the upload isn't an MWPHRF cert at all — different layout, OCR'd
scan, corrupt PDF — ``ParsedCert`` comes back with every field None
plus ``raw_text`` populated for debugging. The boats router treats
"everything None" as a parse-fail and surfaces that to the UI.

Why we tolerate dirty input
---------------------------
Users will upload all sorts of PDFs: ORR, IRC, club-specific format,
old MWPHRF certs (pre-2026 had a different field layout). The MVP only
recognises the 2026 MWPHRF template; anything else returns empty and
the user falls back to manual entry. Adding ORR/IRC parsers is an
orthogonal effort.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

log = logging.getLogger(__name__)


# ─── Output dataclass ────────────────────────────────────────────────


@dataclass
class ParsedCert:
    """Every field we know how to extract from an MWPHRF cert.

    Names match the columns on the ``boats`` table 1:1 so the router
    can ``**parsed.to_boat_payload()`` it into an INSERT. None means
    we couldn't find that field; the user fills it in manually.
    """
    # Identity
    name: Optional[str] = None
    sail_number: Optional[str] = None
    yacht_type: Optional[str] = None
    year: Optional[int] = None
    mwphrf_region: Optional[int] = None
    cert_number: Optional[str] = None
    cert_issued_on: Optional[date] = None

    # Hull
    loa: Optional[float] = None
    lwl: Optional[float] = None
    beam: Optional[float] = None
    draft: Optional[float] = None
    displacement: Optional[float] = None
    engine: Optional[str] = None
    prop_install: Optional[str] = None
    prop_type: Optional[str] = None

    # Rig
    p: Optional[float] = None
    e: Optional[float] = None
    i: Optional[float] = None
    j: Optional[float] = None
    isp: Optional[float] = None
    spl: Optional[float] = None
    jc_tps: Optional[float] = None

    # Handicaps
    hcp: Optional[int] = None
    dhcp: Optional[int] = None
    nshcp: Optional[int] = None
    dnshcp: Optional[int] = None

    # Diagnostics — populated on failure so support can see what came
    # out of pypdf. Not exposed via the API.
    raw_text: str = field(default="", repr=False)

    def found_anything(self) -> bool:
        """True when at least one structured field was parsed.

        Used by the boats router to distinguish "cert parsed cleanly"
        from "we got bytes but couldn't make sense of them"."""
        return any(
            getattr(self, k) is not None
            for k in (
                "name", "sail_number", "hcp", "dhcp", "nshcp", "dnshcp",
                "loa", "p", "i",
            )
        )

    def to_boat_payload(self) -> dict:
        """JSON-ready dict of just the boat-table columns."""
        return {
            k: (v.isoformat() if isinstance(v, date) else v)
            for k, v in self.__dict__.items()
            if k != "raw_text" and v is not None
        }


# ─── Extraction ──────────────────────────────────────────────────────


def parse_mwphrf_cert(pdf_bytes: bytes) -> ParsedCert:
    """Top-level entry point. Returns a fully-populated ParsedCert on
    a recognised cert, or one with every field None on anything we
    can't parse.

    Never raises on bad input — bytes that aren't a PDF, corrupt PDFs,
    PDFs with no extractable text — all return an empty ParsedCert.
    Errors are logged at WARNING.
    """
    try:
        from pypdf import PdfReader
    except ImportError as e:
        log.warning("phrf_cert: pypdf not installed (%s)", e)
        return ParsedCert()

    try:
        from io import BytesIO
        reader = PdfReader(BytesIO(pdf_bytes))
        raw = "\n".join(
            (page.extract_text() or "") for page in reader.pages
        )
    except Exception as e:  # noqa: BLE001 - corrupt PDFs raise many shapes
        log.warning("phrf_cert: pypdf extraction failed (%s)", e)
        return ParsedCert()

    return _parse_text(raw)


def _parse_text(text: str) -> ParsedCert:
    """Pure function over already-extracted text. Split out so tests
    can pass in synthetic strings without round-tripping through
    pypdf."""
    out = ParsedCert(raw_text=text)
    if not text.strip():
        return out

    # "Yacht Name: Gaucho Sail Number: 45367"
    m = re.search(
        r"Yacht\s+Name:\s*(.+?)\s+Sail\s+Number:\s*(\S+)",
        text,
    )
    if m:
        out.name = m.group(1).strip()
        out.sail_number = m.group(2).strip()

    m = re.search(r"Yacht\s+Type:\s*(.+?)(?:\s+Yr\s+of\s+Manufacture|\n|$)", text)
    if m:
        out.yacht_type = m.group(1).strip()

    m = re.search(r"Yr\s+of\s+Manufacture:\s*(\d{4})", text)
    if m:
        try:
            out.year = int(m.group(1))
        except ValueError:
            pass

    m = re.search(r"MWPHRF\s+Region:\s*(\d+)", text)
    if m:
        out.mwphrf_region = int(m.group(1))

    m = re.search(r"Certificate\s*#:\s*(\S+)", text)
    if m:
        out.cert_number = m.group(1).strip()

    m = re.search(r"Certificate\s+Issued\s+on:\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        try:
            out.cert_issued_on = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass

    # Handicaps: four anchored labels.
    for label, attr in (
        (r"ToD\s+buoy\s+racing\s+handicap\s*\(HCP\)", "hcp"),
        (r"ToD\s+random\s+leg\s+handicap\s*\(DHCP\)", "dhcp"),
        (r"ToD\s+non[-\s]?spinnaker\s+handicap\s*\(NSHCP\)", "nshcp"),
        (
            r"ToD\s+random\s+leg\s+non[-\s]?spinnaker\s+handicap\s*\(DNSHCP\)",
            "dnshcp",
        ),
    ):
        m = re.search(rf"{label}\s*:?\s*(-?\d+)", text)
        if m:
            try:
                setattr(out, attr, int(m.group(1)))
            except ValueError:
                pass

    _extract_hull(text, out)
    _extract_rig(text, out)
    return out


# ─── Section extractors ─────────────────────────────────────────────


_HULL_HEADERS = re.compile(
    r"LOA\s+LWL\s+Beam\s+Draft\s+Dispmnt\s+Engine\s+Prop\s+Install\s+Prop\s+Type",
    re.IGNORECASE,
)


def _extract_hull(text: str, out: ParsedCert) -> None:
    """Find the values row that follows the LOA/LWL header. The cert
    template puts five floats then three labels (engine, prop_install,
    prop_type) on the next line. Tokenise on whitespace and pick by
    position with defensive checks."""
    m = _HULL_HEADERS.search(text)
    if not m:
        return
    after = text[m.end():].lstrip()
    tail = after[:200].replace("\n", " ").split()
    floats: list[float] = []
    rest: list[str] = []
    for tok in tail:
        if len(floats) < 5:
            try:
                floats.append(float(tok))
                continue
            except ValueError:
                pass
        rest.append(tok)
    if len(floats) >= 5:
        out.loa, out.lwl, out.beam, out.draft, out.displacement = floats[:5]
    if len(rest) >= 1:
        out.engine = rest[0]
    if len(rest) >= 2:
        out.prop_install = rest[1]
    if len(rest) >= 3:
        out.prop_type = rest[2]


# The cert template flattens the three rig sub-blocks into a single
# header line "P E I J ISP SPL JC_TPS" followed by a single 7-value
# row. We locate that header and read 7 floats positionally.
_RIG_HEADER_ALL = re.compile(
    r"\bP\s+E\s+I\s+J\s+ISP\s+SPL\s+JC_TPS\b"
)


def _extract_rig(text: str, out: ParsedCert) -> None:
    m = _RIG_HEADER_ALL.search(text)
    if not m:
        return
    nums = _next_n_floats(text[m.end():], 7)
    fields = ("p", "e", "i", "j", "isp", "spl", "jc_tps")
    for name, val in zip(fields, nums):
        setattr(out, name, val)


def _next_n_floats(tail: str, n: int) -> list[float]:
    """Pick the next ``n`` float-looking tokens from ``tail``. Skips
    intervening non-numeric tokens (labels, units). Stops at the first
    non-numeric token after ``n`` numbers have been collected."""
    nums: list[float] = []
    for tok in tail[:300].replace("\n", " ").split():
        try:
            nums.append(float(tok))
        except ValueError:
            if nums:
                break
            continue
        if len(nums) >= n:
            break
    return nums
