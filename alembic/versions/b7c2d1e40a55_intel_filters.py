"""add saved intel filters

Revision ID: b7c2d1e40a55
Revises: a4e8f0c91d31
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7c2d1e40a55"
down_revision: str | None = "a4e8f0c91d31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intel_filters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("condition_tree", sa.Text(), nullable=False),
        sa.Column("span_start", sa.String(length=16), nullable=True),
        sa.Column("span_end", sa.String(length=16), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_intel_filters_name", "intel_filters", ["name"])


def downgrade() -> None:
    op.drop_index("ix_intel_filters_name", table_name="intel_filters")
    op.drop_table("intel_filters")
