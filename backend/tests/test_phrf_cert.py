"""Tests for app/services/phrf_cert.py.

The headline test rounds-trips the actual MWPHRF cert we have on file
(tests/fixtures/mwphrf_gaucho.pdf) and asserts every field reads back
correctly. The synthetic-text tests exercise edge cases without
needing pypdf.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.services.phrf_cert import (
    ParsedCert,
    _parse_text,
    parse_mwphrf_cert,
)


FIXTURE = Path(__file__).parent / "fixtures" / "mwphrf_gaucho.pdf"


# ─── End-to-end against the real cert ────────────────────────────────


@pytest.mark.skipif(not FIXTURE.exists(), reason="cert fixture missing")
def test_real_gaucho_cert_parses_completely():
    parsed = parse_mwphrf_cert(FIXTURE.read_bytes())

    # Identity
    assert parsed.name == "Gaucho"
    assert parsed.sail_number == "45367"
    assert parsed.yacht_type == "Beneteau 36.7 First"
    assert parsed.year == 2003
    assert parsed.mwphrf_region == 5
    assert parsed.cert_number == "260327"
    assert parsed.cert_issued_on == date(2026, 3, 5)

    # Handicaps
    assert parsed.hcp == 75
    assert parsed.dhcp == 75
    assert parsed.nshcp == 88
    assert parsed.dnshcp == 88

    # Hull
    assert parsed.loa == pytest.approx(35.0)
    assert parsed.lwl == pytest.approx(30.3)
    assert parsed.beam == pytest.approx(11.6)
    assert parsed.draft == pytest.approx(7.2)
    assert parsed.displacement == pytest.approx(12939)
    assert parsed.engine == "inboard"
    assert parsed.prop_install == "saildrive"
    assert parsed.prop_type == "folding"

    # Rig (positional — these correspond to P/E/I/J/ISP/SPL/JC_TPS)
    assert parsed.p == pytest.approx(45.44)
    assert parsed.e == pytest.approx(15.58)
    assert parsed.i == pytest.approx(45.44)
    assert parsed.j == pytest.approx(13.12)
    assert parsed.isp == pytest.approx(46.26)
    assert parsed.spl == pytest.approx(13.12)
    assert parsed.jc_tps == pytest.approx(0.0)


# ─── Edge cases via synthetic text ───────────────────────────────────


def test_empty_bytes_returns_empty_cert():
    p = parse_mwphrf_cert(b"")
    assert p.found_anything() is False
    assert p.name is None


def test_non_pdf_bytes_returns_empty_cert():
    """Random bytes don't crash; we get an empty cert and a logged warning."""
    p = parse_mwphrf_cert(b"this is not a pdf, just text")
    assert p.found_anything() is False


def test_text_with_only_handicaps_extracts_them():
    text = """
    Some unrelated preamble.
    ToD buoy racing handicap (HCP): 102
    ToD random leg handicap (DHCP): 105
    ToD non-spinnaker handicap (NSHCP): 117
    ToD random leg non-spinnaker handicap (DNSHCP): 120
    """
    p = _parse_text(text)
    assert (p.hcp, p.dhcp, p.nshcp, p.dnshcp) == (102, 105, 117, 120)
    assert p.found_anything() is True


def test_text_with_only_identity_extracts_it():
    text = "Yacht Name: Wind Dancer Sail Number: 12345\nMWPHRF Region: 4"
    p = _parse_text(text)
    assert p.name == "Wind Dancer"
    assert p.sail_number == "12345"
    assert p.mwphrf_region == 4


def test_negative_handicap_parsed():
    """Fast PHRF boats can carry negative ratings; the parser
    shouldn't trip the regex on the minus sign."""
    text = "ToD buoy racing handicap (HCP): -3"
    p = _parse_text(text)
    assert p.hcp == -3


def test_to_boat_payload_omits_none_and_serialises_dates():
    p = ParsedCert(
        name="X", hcp=75, cert_issued_on=date(2026, 3, 5),
    )
    payload = p.to_boat_payload()
    assert payload == {
        "name": "X",
        "hcp": 75,
        "cert_issued_on": "2026-03-05",
    }


def test_found_anything_false_on_empty():
    assert ParsedCert().found_anything() is False


def test_found_anything_true_on_just_a_rating():
    assert ParsedCert(hcp=75).found_anything() is True
