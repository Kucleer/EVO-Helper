"""复查 scope 列放宽到 32 字

Revision ID: b4d81f60c9a2
Revises: e29d06f8489f
Create Date: 2026-08-09

调度器要往 `target_revisits.scope` 写 `BOT_TIER_NEGLIGIBLE`（20 字）与
`BOT_REPORT_MISSING`（18 字），都超过原先声明的 16。

SQLite 不校验 VARCHAR 长度，超了也照存不误——正因为现在不报错，这条迁移
才更该有：声明与实际存的东西对不上，换到任何一个真会校验的库上就是一批
截断或插入失败，而那时候没人会想起来是这里。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b4d81f60c9a2"
down_revision: str | None = "e29d06f8489f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("target_revisits") as batch:
        batch.alter_column("scope", type_=sa.String(32), existing_type=sa.String(16))


def downgrade() -> None:
    with op.batch_alter_table("target_revisits") as batch:
        batch.alter_column("scope", type_=sa.String(16), existing_type=sa.String(32))
