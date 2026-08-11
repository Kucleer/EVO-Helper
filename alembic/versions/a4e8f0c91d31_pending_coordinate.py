"""persist pending coordinate for safe capacity waits

Revision ID: a4e8f0c91d31
Revises: 8c41b9d201ff
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4e8f0c91d31"
down_revision: str | None = "8c41b9d201ff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("run_instances") as batch:
        batch.add_column(sa.Column("pending_galaxy", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pending_system", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pending_position", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("run_instances") as batch:
        batch.drop_column("pending_position")
        batch.drop_column("pending_system")
        batch.drop_column("pending_galaxy")
