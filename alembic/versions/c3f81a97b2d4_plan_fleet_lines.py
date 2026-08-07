"""add fleet line limit and reserved lines to scan plans

Revision ID: c3f81a97b2d4
Revises: b7c2d1e40a55
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f81a97b2d4"
down_revision: str | None = "b7c2d1e40a55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("scan_plans") as batch:
        # Existing plans keep the previous behaviour: one line, none reserved.
        batch.add_column(
            sa.Column("fleet_line_limit", sa.Integer(), nullable=False, server_default="1")
        )
        batch.add_column(
            sa.Column("reserved_lines", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("scan_plans") as batch:
        batch.drop_column("reserved_lines")
        batch.drop_column("fleet_line_limit")
