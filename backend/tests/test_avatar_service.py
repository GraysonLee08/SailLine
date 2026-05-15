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
