"""add gist index on track_points.position

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-07
"""
from alembic import op


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "track_position_idx",
        "track_points",
        ["position"],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("track_position_idx", table_name="track_points")