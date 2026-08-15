"""keep the ranking rank so the descending checksum survives ingest

Revision ID: f3a91c2d4e07
Revises: e8b7c1d23a40
Create Date: 2026-08-15
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a91c2d4e07"
down_revision: str | None = "e8b7c1d23a40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("bot_targets") as batch:
        batch.add_column(sa.Column("military_rank", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("bot_targets") as batch:
        batch.drop_column("military_rank")
