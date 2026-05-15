"""User avatar processing + Cloud Storage upload.

Pure-function ``process_avatar`` validates and resizes the upload to a
fixed 256×256 WebP — small enough that the crew-list thumbnail and the
ProfileView header preview both render off the same file, large enough
that we don't lose detail on a retina display. WebP at quality 85
gives us roughly a 10× smaller payload than the source JPEG most users
will upload.

The GCS side is best-effort, mirroring the cert-upload pattern in
``app/routers/boats.py``: when ``GCS_AVATARS_BUCKET`` isn't set (local
dev), ``store_avatar`` returns ``None`` and the router skips the
``avatar_url`` update. When set, we write to a deterministic blob
name (``{uid}.webp``) so each upload overwrites — no orphan files to
garbage-collect, and the public URL stays stable. Browsers cache that
URL aggressively; the router cache-busts by appending ``?v={epoch}``.

Bucket convention: public-read, served via
``https://storage.googleapis.com/{bucket}/{uid}.webp``. CORS only
matters if we ever load these as ``crossorigin`` for Canvas — we don't,
the frontend uses plain ``<img src>``, so a vanilla public bucket
without explicit CORS works.

Why not a signed URL on each render?  Because the avatars are
intentionally public — crew see each other's avatars across boats, and
nothing in them is sensitive. The user opts in by uploading.
"""

from __future__ import annotations

import io
import logging
from typing import Optional

log = logging.getLogger(__name__)

# Frontend hard-caps upload size at the same value; the server is the
# authority though. 5 MB is well over what a downsized phone photo
# needs and small enough to keep the request handler quick.
MAX_UPLOAD_BYTES = 5 * 1024 * 1024

# Output size + format. 256×256 is the largest the crew row uses; the
# ProfileView preview is also 256 so a single file serves both.
AVATAR_SIZE = 256
AVATAR_FORMAT = "WEBP"
AVATAR_QUALITY = 85

# MIME types we'll accept from the multipart upload. Pillow can read
# more than this (TIFF, BMP, etc.) but we want to keep the surface area
# small — phones produce HEIC / JPEG / PNG / WebP, and that's it.
ALLOWED_INPUT_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)


class AvatarProcessingError(ValueError):
    """Raised when validation or Pillow processing fails.

    The router maps this to HTTP 400 with the message echoed to the
    user, so keep messages short and user-facing.
    """


def process_avatar(contents: bytes, content_type: str | None) -> bytes:
    """Validate + resize to a 256×256 WebP. Returns the encoded bytes.

    Steps:
      1. Length check (caller has usually already done this, but defend
         in depth — Pillow on a 100 MB file is its own DoS).
      2. MIME allow-list. We don't blindly trust the client header,
         but a wrong header is a strong signal of confused intent —
         reject early before paying the Pillow decode cost.
      3. Decode through Pillow. Anything Pillow can't parse raises.
      4. EXIF-respecting orientation fix so iPhone landscape selfies
         don't end up sideways.
      5. Center-crop to a square, then resize to 256×256 with LANCZOS
         (the highest-quality downscaler Pillow ships).
      6. Flatten alpha onto white (some PNGs ship transparent
         backgrounds; WebP keeps alpha but the crew thumbnail looks
         better with an opaque circle). Apply only if the source has
         alpha — keeping opaque sources lossless on the alpha axis.
      7. Encode to WebP quality 85.
    """
    if not contents:
        raise AvatarProcessingError("empty upload")
    if len(contents) > MAX_UPLOAD_BYTES:
        raise AvatarProcessingError(
            f"image too large (>{MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"
        )
    if content_type and content_type not in ALLOWED_INPUT_TYPES:
        raise AvatarProcessingError(
            f"unsupported image type: {content_type}"
        )

    # Lazy import keeps the request handler from paying Pillow's
    # import cost on every non-avatar request, and lets unit tests run
    # without Pillow installed when only schema-level tests are
    # exercised.
    try:
        from PIL import Image, ImageOps  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - import guard
        raise AvatarProcessingError(
            "image processing unavailable (Pillow missing)"
        ) from exc

    try:
        img = Image.open(io.BytesIO(contents))
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise AvatarProcessingError("could not decode image") from exc

    # Apply EXIF orientation so portrait phone photos land right-side up.
    img = ImageOps.exif_transpose(img)

    # Square crop centred on the image, then resize. ImageOps.fit does
    # exactly this in one call.
    img = ImageOps.fit(
        img,
        (AVATAR_SIZE, AVATAR_SIZE),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )

    # Flatten alpha to white when present — WebP supports alpha but
    # the typical avatar lands inside a circle mask in the UI, so an
    # opaque background renders more predictably across themes.
    if img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    ):
        background = Image.new("RGB", img.size, (255, 255, 255))
        img_rgba = img.convert("RGBA")
        background.paste(img_rgba, mask=img_rgba.split()[3])
        img = background
    else:
        img = img.convert("RGB")

    out = io.BytesIO()
    img.save(out, format=AVATAR_FORMAT, quality=AVATAR_QUALITY, method=4)
    return out.getvalue()


def store_avatar(processed: bytes, uid: str) -> Optional[str]:
    """Upload the processed bytes to GCS. Returns the public URL or None.

    Bucket name comes from the ``GCS_AVATARS_BUCKET`` env var (see
    ``app/config.py``). When unset (local dev), we log and return None;
    the router treats that as "skip the avatar_url write" so the rest
    of the profile-update flow still succeeds.

    Failures (auth, network, quota) also return None — avatar storage
    is non-critical and shouldn't take down a profile save. The error
    is logged at WARNING so production has a paper trail.
    """
    # Defer the settings import so tests that don't exercise this
    # path don't pay the pydantic load cost.
    from app.config import get_settings

    settings = get_settings()
    bucket_name = settings.gcs_avatars_bucket
    if not bucket_name:
        log.info("avatars: GCS_AVATARS_BUCKET not set; skipping upload")
        return None

    try:
        from google.cloud import storage  # type: ignore[import-not-found]

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob_name = f"{uid}.webp"
        blob = bucket.blob(blob_name)
        blob.cache_control = "public, max-age=86400"
        blob.upload_from_string(processed, content_type="image/webp")
        return f"https://storage.googleapis.com/{bucket_name}/{blob_name}"
    except Exception as exc:  # noqa: BLE001
        log.warning("avatars: GCS upload failed (%s)", exc)
        return None


def delete_avatar(uid: str) -> None:
    """Best-effort delete of the user's avatar blob.

    Called from ``DELETE /api/users/me/avatar``. The DB column is
    cleared first by the router; this just tidies up the bucket. We
    swallow all errors because the user has already seen a successful
    clear in the UI — a transient GCS hiccup shouldn't surface as a
    failure.
    """
    from app.config import get_settings

    settings = get_settings()
    bucket_name = settings.gcs_avatars_bucket
    if not bucket_name:
        return
    try:
        from google.cloud import storage  # type: ignore[import-not-found]

        client = storage.Client()
        bucket = client.bucket(bucket_name)
        bucket.blob(f"{uid}.webp").delete()
    except Exception as exc:  # noqa: BLE001
        log.warning("avatars: GCS delete failed (%s)", exc)
