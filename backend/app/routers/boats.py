"""Boats — first-class boat entities with PHRF cert metadata.

CRUD for the authenticated user's boats, plus a cert-upload endpoint
that parses an MWPHRF cert PDF and returns the extracted fields so the
frontend can pre-fill the boat editor.

Auth: every route is owner-scoped via Firebase uid. A boat belongs to
exactly one user; sharing-with-crew is D3 work and changes the auth
predicate from ``owner_id = uid`` to ``EXISTS (... boat_crew ...)``.

Schema notes:
  * The ``boats`` table (migration 0011) has nullable handicap fields,
    so a boat row can describe a one-design boat or a partial entry.
  * On boat delete we also try to delete the cert PDF from GCS, best
    effort. A failure there logs at WARNING but the row still goes.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional
from uuid import UUID

import asyncpg
from fastapi import (
    APIRouter, Depends, File, HTTPException, UploadFile, status,
)
from pydantic import BaseModel, Field

from app import db
from app.auth import get_current_user
from app.auth_helpers import boat_owner_predicate, boat_read_predicate
from app.config import get_settings
from app.services.phrf_cert import parse_mwphrf_cert

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/boats", tags=["boats"])


# 5 MB upload cap. Real MWPHRF certs are <50 KB; anything bigger is
# almost certainly a misuse of the endpoint.
_MAX_CERT_BYTES = 5 * 1024 * 1024


# ─── Pydantic models ─────────────────────────────────────────────────


class BoatBase(BaseModel):
    """Writable boat fields. All optional so partial updates work."""
    name: Optional[str] = None
    sail_number: Optional[str] = None
    yacht_type: Optional[str] = None
    year: Optional[int] = None
    mwphrf_region: Optional[int] = None
    loa: Optional[float] = None
    lwl: Optional[float] = None
    beam: Optional[float] = None
    draft: Optional[float] = None
    displacement: Optional[float] = None
    engine: Optional[str] = None
    prop_install: Optional[str] = None
    prop_type: Optional[str] = None
    p: Optional[float] = None
    e: Optional[float] = None
    i: Optional[float] = None
    j: Optional[float] = None
    isp: Optional[float] = None
    spl: Optional[float] = None
    jc_tps: Optional[float] = None
    hcp: Optional[int] = None
    dhcp: Optional[int] = None
    nshcp: Optional[int] = None
    dnshcp: Optional[int] = None
    cert_number: Optional[str] = None
    # Pydantic coerces ISO strings ("2026-03-05") to date here, which
    # in turn lets asyncpg bind the DATE column without an explicit
    # cast. Posting a string from the frontend works as long as it
    # parses as ISO-8601.
    cert_issued_on: Optional[date] = None
    cert_pdf_gcs_url: Optional[str] = None


class BoatCreate(BoatBase):
    """At least ``name`` must be set on create."""
    name: str = Field(min_length=1, max_length=200)


class BoatOut(BoatBase):
    id: UUID
    owner_id: str
    name: str
    created_at: str
    updated_at: str


class ParsedCertOut(BaseModel):
    """Response from POST /api/boats/{id}/cert."""
    parsed: dict
    stored_url: Optional[str] = None
    parse_succeeded: bool


# ─── DB helpers ─────────────────────────────────────────────────────


_BOAT_COLS = (
    "id, owner_id, name, sail_number, yacht_type, year, mwphrf_region, "
    "loa, lwl, beam, draft, displacement, engine, prop_install, prop_type, "
    "p, e, i, j, isp, spl, jc_tps, "
    "hcp, dhcp, nshcp, dnshcp, "
    "cert_number, cert_issued_on, cert_pdf_gcs_url, "
    "created_at, updated_at"
)


def _row_to_out(row: asyncpg.Record) -> BoatOut:
    d = dict(row)
    # Coerce dates/datetimes to ISO strings.
    if d.get("cert_issued_on") is not None:
        d["cert_issued_on"] = d["cert_issued_on"].isoformat()
    if d.get("created_at") is not None:
        d["created_at"] = d["created_at"].isoformat()
    if d.get("updated_at") is not None:
        d["updated_at"] = d["updated_at"].isoformat()
    # NUMERIC comes back as Decimal — coerce to float for JSON.
    for k in (
        "loa", "lwl", "beam", "draft", "displacement",
        "p", "e", "i", "j", "isp", "spl", "jc_tps",
    ):
        if d.get(k) is not None:
            d[k] = float(d[k])
    return BoatOut(**d)


async def _load_readable(
    conn: asyncpg.Connection, boat_id: UUID, uid: str,
) -> asyncpg.Record:
    """404 unless caller can READ the boat (creator OR any crew role)."""
    pred = boat_read_predicate(boat_alias="b", uid_placeholder="$2")
    cols_b = ", ".join(f"b.{c.strip()}" for c in _BOAT_COLS.split(","))
    row = await conn.fetchrow(
        f"SELECT {cols_b} FROM boats b WHERE b.id = $1 AND {pred}",
        boat_id, uid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "boat not found")
    return row


async def _load_owned(
    conn: asyncpg.Connection, boat_id: UUID, uid: str,
) -> asyncpg.Record:
    """404 unless caller OWNS the boat (boats.owner_id OR boat_crew
    role='owner'). Used for write operations."""
    pred = boat_owner_predicate(boat_alias="b", uid_placeholder="$2")
    cols_b = ", ".join(f"b.{c.strip()}" for c in _BOAT_COLS.split(","))
    row = await conn.fetchrow(
        f"SELECT {cols_b} FROM boats b WHERE b.id = $1 AND {pred}",
        boat_id, uid,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "boat not found")
    return row


# ─── CRUD ───────────────────────────────────────────────────────────


@router.get("", response_model=list[BoatOut])
async def list_boats(
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """List boats the caller can see: owned + any membership."""
    pred = boat_read_predicate(boat_alias="b", uid_placeholder="$1")
    cols_b = ", ".join(f"b.{c.strip()}" for c in _BOAT_COLS.split(","))
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT {cols_b} FROM boats b WHERE {pred} "
            "ORDER BY b.created_at DESC",
            user["uid"],
        )
    return [_row_to_out(r) for r in rows]


@router.post("", response_model=BoatOut, status_code=status.HTTP_201_CREATED)
async def create_boat(
    payload: BoatCreate,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    data = payload.model_dump(exclude_none=True)
    fields = list(data.keys())
    placeholders = [f"${i + 2}" for i in range(len(fields))]
    cols = ", ".join(fields)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"""
            INSERT INTO boats (owner_id, {cols})
            VALUES ($1, {", ".join(placeholders)})
            RETURNING {_BOAT_COLS}
            """,
            user["uid"], *[data[f] for f in fields],
        )
    return _row_to_out(row)


@router.get("/{boat_id}", response_model=BoatOut)
async def get_boat(
    boat_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Any member can read the boat record."""
    async with pool.acquire() as conn:
        row = await _load_readable(conn, boat_id, user["uid"])
    return _row_to_out(row)


@router.patch("/{boat_id}", response_model=BoatOut)
async def update_boat(
    boat_id: UUID,
    payload: BoatBase,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    data = payload.model_dump(exclude_none=True)
    if not data:
        # Touch the row so updated_at advances; useful for the
        # frontend to know the user "confirmed" the boat record
        # without changing anything.
        async with pool.acquire() as conn:
            row = await _load_owned(conn, boat_id, user["uid"])
        return _row_to_out(row)

    sets = [f"{k} = ${i + 3}" for i, k in enumerate(data.keys())]
    async with pool.acquire() as conn:
        # Auth check first (cheap), then update.
        await _load_owned(conn, boat_id, user["uid"])
        row = await conn.fetchrow(
            f"""
            UPDATE boats
            SET {", ".join(sets)}, updated_at = NOW()
            WHERE id = $1 AND owner_id = $2
            RETURNING {_BOAT_COLS}
            """,
            boat_id, user["uid"], *data.values(),
        )
    return _row_to_out(row)


@router.delete("/{boat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_boat(
    boat_id: UUID,
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    async with pool.acquire() as conn:
        row = await _load_owned(conn, boat_id, user["uid"])
        await conn.execute(
            "DELETE FROM boats WHERE id = $1 AND owner_id = $2",
            boat_id, user["uid"],
        )
    # Best-effort cert PDF cleanup. Failure here doesn't undo the
    # delete — the row is already gone.
    gcs_url = row["cert_pdf_gcs_url"]
    if gcs_url:
        _try_delete_gcs(gcs_url)
    return None


# ─── Cert upload ────────────────────────────────────────────────────


@router.post("/{boat_id}/cert", response_model=ParsedCertOut)
async def upload_cert(
    boat_id: UUID,
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
    pool: asyncpg.Pool = Depends(db.get_pool),
):
    """Accept a PHRF cert PDF, parse it, optionally store it in GCS.

    Returns the parsed fields plus the storage URL (or None when GCS
    isn't configured). The frontend uses the parsed dict to pre-fill
    the boat editor; the user reviews and PATCHes the boat record
    with whatever they accept.

    Storing the PDF is independent of saving the parsed fields to the
    DB. The user has to explicitly PATCH the boat with the values they
    want; this endpoint is purely a "parse + stash" helper.
    """
    # Ownership check before reading the upload.
    async with pool.acquire() as conn:
        await _load_owned(conn, boat_id, user["uid"])

    contents = await file.read()
    if len(contents) > _MAX_CERT_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"cert too large (>{_MAX_CERT_BYTES // (1024 * 1024)} MB)",
        )
    if not contents:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")

    parsed = parse_mwphrf_cert(contents)
    stored_url = _try_store_gcs(contents, user["uid"], boat_id)

    # If we successfully stored, persist the URL on the row so the
    # frontend can later let the user re-download the original.
    if stored_url:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE boats SET cert_pdf_gcs_url = $1, updated_at = NOW() "
                "WHERE id = $2 AND owner_id = $3",
                stored_url, boat_id, user["uid"],
            )

    return ParsedCertOut(
        parsed=parsed.to_boat_payload(),
        stored_url=stored_url,
        parse_succeeded=parsed.found_anything(),
    )


# ─── GCS helpers ────────────────────────────────────────────────────


def _try_store_gcs(
    contents: bytes, uid: str, boat_id: UUID,
) -> Optional[str]:
    """Store the PDF under ``gs://{bucket}/{uid}/{boat_id}.pdf``.

    Returns the ``gs://`` URI on success, None when the bucket isn't
    configured or storage fails. Failures log at WARNING — the cert
    PARSING is the user-facing value-add, so storage is best-effort.
    """
    settings = get_settings()
    bucket_name = settings.gcs_certs_bucket
    if not bucket_name:
        log.info("boats: GCS_CERTS_BUCKET not set; skipping cert store")
        return None
    try:
        from google.cloud import storage  # type: ignore[import-not-found]
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob_name = f"{uid}/{boat_id}.pdf"
        blob = bucket.blob(blob_name)
        blob.upload_from_string(contents, content_type="application/pdf")
        return f"gs://{bucket_name}/{blob_name}"
    except Exception as e:  # noqa: BLE001
        log.warning("boats: GCS upload failed (%s)", e)
        return None


def _try_delete_gcs(gcs_url: str) -> None:
    """Delete the cert PDF if we recognise the URL. Best effort."""
    if not gcs_url.startswith("gs://"):
        return
    try:
        path = gcs_url[len("gs://"):]
        bucket_name, _, blob_name = path.partition("/")
        if not (bucket_name and blob_name):
            return
        from google.cloud import storage  # type: ignore[import-not-found]
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        bucket.blob(blob_name).delete()
    except Exception as e:  # noqa: BLE001
        log.warning("boats: GCS delete failed for %s (%s)", gcs_url, e)
