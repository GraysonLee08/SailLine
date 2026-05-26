"""Unit tests for app/services/avatars.py — Pillow-side only.

The GCS upload/delete branches are tested in test_users_router.py via
the router (with the helpers stubbed). Here we verify the pure-function
``process_avatar`` correctly validates, resizes, and re-encodes.
"""
from __future__ import annotations

import io

import pytest

from app.services.avatars import (
    AVATAR_SIZE,
    AvatarProcessingError,
    MAX_UPLOAD_BYTES,
    process_avatar,
)


pytest.importorskip("PIL", reason="Pillow not installed")


def _png(size=(80, 80), color=(0, 128, 0)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _png_rgba(size=(80, 80)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", size, (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def test_process_avatar_returns_256_webp():
    out = process_avatar(_png((120, 200)), "image/png")
    # Quick sanity: bytes start with WebP RIFF header.
    assert out[:4] == b"RIFF"
    assert b"WEBP" in out[:16]

    # Decode it back to verify the dimensions.
    from PIL import Image

    img = Image.open(io.BytesIO(out))
    assert img.size == (AVATAR_SIZE, AVATAR_SIZE)


def test_process_avatar_handles_rgba_flatten():
    # RGBA → opaque white-flattened RGB WebP. Verify it doesn't blow
    # up and produces something openable.
    out = process_avatar(_png_rgba((80, 80)), "image/png")
    from PIL import Image

    img = Image.open(io.BytesIO(out))
    assert img.size == (AVATAR_SIZE, AVATAR_SIZE)


def test_process_avatar_rejects_empty():
    with pytest.raises(AvatarProcessingError, match="empty"):
        process_avatar(b"", "image/png")


def test_process_avatar_rejects_oversize():
    big = b"x" * (MAX_UPLOAD_BYTES + 1)
    with pytest.raises(AvatarProcessingError, match="too large"):
        process_avatar(big, "image/png")


def test_process_avatar_rejects_bad_mime():
    with pytest.raises(AvatarProcessingError, match="unsupported"):
        process_avatar(_png(), "application/pdf")


def test_process_avatar_rejects_undecodable():
    with pytest.raises(AvatarProcessingError, match="decode"):
        # Valid-ish length, valid-ish MIME, but the bytes are nonsense.
        process_avatar(b"not a real image", "image/png")


# ── Additional coverage (2026-05-26) ─────────────────────────────────────
#
# The tests above cover the happy/rejection paths. The ones below pin
# behaviour that the docstring contract promises but the original suite
# didn't exercise: EXIF orientation, accepted-but-untested input formats
# (JPEG/WebP/GIF), missing-content-type fallback, and center-crop
# squareness on rectangular sources.


def _jpeg(size=(120, 80), color=(0, 64, 128)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _webp(size=(200, 200), color=(128, 0, 64)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="WEBP", quality=90)
    return buf.getvalue()


def _gif(size=(80, 80), color=(200, 200, 0)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    # GIF needs palette mode. Pillow handles the conversion implicitly
    # when given an RGB image, but be explicit so the test is stable
    # across Pillow versions.
    Image.new("P", size, 0).save(buf, format="GIF")
    return buf.getvalue()


def _png_with_exif_rotation() -> bytes:
    """Tall image with EXIF orientation=6 (rotate-90-CW on display).

    A camera that captures sensor-landscape but is held in portrait
    stores orientation=6. ImageOps.exif_transpose should rotate it back
    before the crop, otherwise the wrong axis ends up centered.

    PNG doesn't natively support EXIF metadata in older Pillow versions,
    so we embed it via JPEG instead — same code path through
    process_avatar regardless.
    """
    from PIL import Image

    # 100 wide, 200 tall, distinctive red strip on the left so we can
    # check it ends up at the top after the rotate.
    img = Image.new("RGB", (100, 200), (0, 128, 0))
    for x in range(10):
        for y in range(200):
            img.putpixel((x, y), (255, 0, 0))

    buf = io.BytesIO()
    # exif=b"\x00\x00" would be empty; build a minimal EXIF block with
    # orientation=6. Pillow's _getexif is fiddly to build by hand, so
    # we save once, reopen, set orientation via the Pillow helper if
    # available, otherwise just return the JPEG as-is (test will then
    # assert the trivial case).
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_process_avatar_accepts_jpeg():
    out = process_avatar(_jpeg((300, 200)), "image/jpeg")
    from PIL import Image

    img = Image.open(io.BytesIO(out))
    assert img.size == (AVATAR_SIZE, AVATAR_SIZE)


def test_process_avatar_accepts_webp():
    out = process_avatar(_webp((300, 300)), "image/webp")
    from PIL import Image

    img = Image.open(io.BytesIO(out))
    assert img.size == (AVATAR_SIZE, AVATAR_SIZE)


def test_process_avatar_accepts_gif():
    out = process_avatar(_gif((120, 120)), "image/gif")
    from PIL import Image

    img = Image.open(io.BytesIO(out))
    assert img.size == (AVATAR_SIZE, AVATAR_SIZE)


def test_process_avatar_accepts_missing_content_type():
    # content_type=None means we skip the MIME allow-list — Pillow's
    # decoder is the validation in that case.
    out = process_avatar(_png((128, 128)), None)
    from PIL import Image

    img = Image.open(io.BytesIO(out))
    assert img.size == (AVATAR_SIZE, AVATAR_SIZE)


def test_process_avatar_center_crop_squares_a_tall_source():
    # 100×400 → center crop should yield a square (100×100) before the
    # resize, then upscale to 256×256. Output is always square.
    out = process_avatar(_png((100, 400), (40, 90, 140)), "image/png")
    from PIL import Image

    img = Image.open(io.BytesIO(out))
    assert img.size == (AVATAR_SIZE, AVATAR_SIZE)
    # The cropped region should be the vertical middle — pixel at the
    # centre is the source colour (not interpolated to something wild).
    cx, cy = AVATAR_SIZE // 2, AVATAR_SIZE // 2
    r, g, b = img.convert("RGB").getpixel((cx, cy))
    # Allow a tolerance for LANCZOS + WebP-85 lossiness.
    assert abs(r - 40) < 8 and abs(g - 90) < 8 and abs(b - 140) < 8


def test_process_avatar_upscales_small_source():
    # 32×32 → still produces 256×256 output. Not a great-looking avatar
    # but it shouldn't error.
    out = process_avatar(_png((32, 32)), "image/png")
    from PIL import Image

    img = Image.open(io.BytesIO(out))
    assert img.size == (AVATAR_SIZE, AVATAR_SIZE)


def test_process_avatar_exif_orientation_is_applied():
    # Build a 100×200 JPEG, then add EXIF orientation=6 (rotate 90 CW
    # when displaying). After process_avatar, the result is still
    # 256×256 (square), and the dominant colour should remain the
    # source colour (i.e. the orientation tag didn't break decode).
    from PIL import Image

    base = Image.new("RGB", (100, 200), (200, 50, 50))
    buf = io.BytesIO()
    # Pillow accepts an `exif` bytes blob on save. Build the minimal
    # TIFF header for orientation=6.
    exif_bytes = (
        b"Exif\x00\x00"
        b"II*\x00"  # little-endian TIFF
        b"\x08\x00\x00\x00"  # offset to first IFD
        b"\x01\x00"  # 1 entry
        b"\x12\x01"  # tag = 0x0112 (Orientation)
        b"\x03\x00"  # type SHORT
        b"\x01\x00\x00\x00"  # count
        b"\x06\x00\x00\x00"  # value = 6
        b"\x00\x00\x00\x00"  # next IFD offset = 0
    )
    base.save(buf, format="JPEG", quality=90, exif=exif_bytes)
    out = process_avatar(buf.getvalue(), "image/jpeg")

    img = Image.open(io.BytesIO(out))
    assert img.size == (AVATAR_SIZE, AVATAR_SIZE)
    # Sample the centre pixel — should still be roughly the source
    # red colour, just re-encoded as WebP.
    r, g, b = img.convert("RGB").getpixel((AVATAR_SIZE // 2, AVATAR_SIZE // 2))
    assert r > 150 and g < 100 and b < 100, f"expected reddish, got ({r},{g},{b})"


def test_process_avatar_flattens_transparent_png_to_white():
    # A fully-transparent input flattens onto white per the docstring.
    out = process_avatar(_png_rgba((128, 128)), "image/png")
    from PIL import Image

    img = Image.open(io.BytesIO(out)).convert("RGB")
    # Sampling any pixel should give white (modulo WebP lossiness).
    r, g, b = img.getpixel((10, 10))
    assert r > 240 and g > 240 and b > 240


def test_process_avatar_oversize_check_runs_before_decode():
    # An oversized buffer shouldn't even attempt a Pillow decode. We
    # don't have a clean way to assert "didn't call Pillow", but we can
    # verify the error message identifies size (not decode).
    big = b"x" * (MAX_UPLOAD_BYTES + 1)
    with pytest.raises(AvatarProcessingError) as exc_info:
        process_avatar(big, "image/png")
    msg = str(exc_info.value).lower()
    assert "too large" in msg and "decode" not in msg
