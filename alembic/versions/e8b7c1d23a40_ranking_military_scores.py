"""persist military-ranking evidence on bot targets

Revision ID: e8b7c1d23a40
Revises: d2c4b8a71f39
Create Date: 2026-08-14
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e8b7c1d23a40"
down_revision: str | None = "d2c4b8a71f39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 供时区迁移测试逐列对照；新列不能悄悄落成无时区时间。
_TIMESTAMP_COLUMNS = (("bot_targets", "military_score_at_utc"),)


def upgrade() -> None:
    with op.batch_alter_table("bot_targets") as batch:
        batch.add_column(sa.Column("source", sa.String(length=16), nullable=False, server_default="scan"))
        batch.add_column(sa.Column("military_score", sa.Float(), nullable=True))
        batch.add_column(sa.Column("military_score_at_utc", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column("military_score_estimated", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("bot_targets") as batch:
        batch.drop_column("military_score_estimated")
        batch.drop_column("military_score_at_utc")
        batch.drop_column("military_score")
        batch.drop_column("source")
