"""add boats table

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-14

Boats become first-class entities in Session D2. The table mirrors the
fields on a 2026 MWPHRF Handicap Certificate (see
``backend/app/services/phrf_cert.py`` for the parser), plus the four
race handicaps used for ToD scoring on the post-race stats view.

All cert / dimensions fields are nullable so one row works equally well
for:

  * a fully-rated keelboat with a complete MWPHRF cert
  * a one-design boat that doesn't carry a PHRF rating (handicaps stay
    NULL; stats endpoint omits the corrected-time column)
  * a boat the user only entered a name for (everything else NULL until
    they upload a cert)

``cert_pdf_gcs_url`` is a free-form URL because we want the option to
store somewhere other than GCS later (signed S3 link, user-supplied
external link, etc.). v1 stores it under
``gs://sailline-certs/<owner_uid>/<boat_id>.pdf`` — see
``backend/app/routers/boats.py``.

NOT NULL on: id, owner_id, name. Everything else nullable. Numeric uses
NUMERIC so cert values keep their decimal precision (LOA is "35.00",
not 35).
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Create boats."""
    op.execute(
        """
        CREATE TABLE boats (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id      TEXT NOT NULL
                          REFERENCES user_profiles(id) ON DELETE CASCADE,
            name          TEXT NOT NULL,
            sail_number   TEXT,
            yacht_type    TEXT,
            year          INT,
            mwphrf_region INT,

            -- Hull dims (informational; not used for routing — that's
            -- the boat_class polar lookup). Stored for completeness so
            -- the cert PDF round-trips cleanly.
            loa           NUMERIC,
            lwl           NUMERIC,
            beam          NUMERIC,
            draft         NUMERIC,
            displacement  NUMERIC,
            engine        TEXT,
            prop_install  TEXT,
            prop_type     TEXT,

            -- Rig dims (P/E mainsail, I/J headsail foretriangle,
            -- ISP/SPL spinnaker pole, JC_TPS spinnaker tack offset).
            p             NUMERIC,
            e             NUMERIC,
            i             NUMERIC,
            j             NUMERIC,
            isp           NUMERIC,
            spl           NUMERIC,
            jc_tps        NUMERIC,

            -- The four ToD handicaps in seconds per nautical mile.
            -- NULL = boat does not carry that rating; the stats math
            -- skips corrected time when the relevant rating is null.
            hcp           INT,  -- ToD buoy with spinnaker
            dhcp          INT,  -- ToD random-leg with spinnaker
            nshcp         INT,  -- ToD buoy non-spinnaker
            dnshcp        INT,  -- ToD random-leg non-spinnaker

            -- Cert metadata.
            cert_number      TEXT,
            cert_issued_on   DATE,
            cert_pdf_gcs_url TEXT,

            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX boats_owner_idx
            ON boats(owner_id, created_at DESC)
        """
    )


def downgrade() -> None:
    """Reverse the boats table. Destructive."""
    op.execute("DROP INDEX IF EXISTS boats_owner_idx")
    op.execute("DROP TABLE IF EXISTS boats")
