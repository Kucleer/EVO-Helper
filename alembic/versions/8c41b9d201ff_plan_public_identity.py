"""add persistent public identities to scan plans

Revision ID: 8c41b9d201ff
Revises: 28376b48e201
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8c41b9d201ff"
down_revision: str | None = "28376b48e201"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite cannot add a non-null UUID column to an occupied table. Add nullable,
    # backfill each existing row, then rebuild the table with the final constraints.
    with op.batch_alter_table("scan_plans") as batch:
        batch.add_column(sa.Column("public_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("updated_at_utc", sa.DateTime(), nullable=True))
    op.execute(
        "UPDATE scan_plans SET public_id = lower(hex(randomblob(16))), "
        "updated_at_utc = created_at_utc WHERE public_id IS NULL"
    )
    with op.batch_alter_table("scan_plans") as batch:
        batch.alter_column("public_id", nullable=False)
        batch.alter_column("updated_at_utc", nullable=False)
        batch.create_unique_constraint("uq_scan_plans_public_id", ["public_id"])
        batch.create_index("ix_scan_plans_public_id", ["public_id"], unique=True)


def downgrade() -> None:
    with op.batch_alter_table("scan_plans") as batch:
        batch.drop_index("ix_scan_plans_public_id")
        batch.drop_constraint("uq_scan_plans_public_id", type_="unique")
        batch.drop_column("updated_at_utc")
        batch.drop_column("public_id")
