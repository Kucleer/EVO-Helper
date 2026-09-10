"""修正 recycle_* 时刻列的时区

Revision ID: b8d4f6e0a3c2
Revises: a7c3e5d9f2b1
Create Date: 2026-09-10

⚠️ Bug 2 修复：迁移 `e5a8c3d2f1b4` 把两张新表的时刻列建成了
`timestamp WITHOUT time zone`（`sa.DateTime()`），而本仓其余时刻列用的是
`UTCDateTime`（`TIMESTAMP WITH TIME ZONE`）。

后果：写进去的 aware UTC 被按会话时区转成 +08 墙钟再丢掉时区 ——
库里那条存的是 `21:13:30`，而当时 UTC 是 `13:13:30`。
现在靠「写和读走同一个会话时区」侥幸抵消掉了，但会话时区一变就错 8 小时。

修法：改成 `DateTime(timezone=True)`，已有行按 `+08 → UTC` 修正。
⚠️ SQLite 上改列要 batch mode。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d4f6e0a3c2"
down_revision: str | Sequence[str] | None = "a7c3e5d9f2b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # PostgreSQL：直接 ALTER TYPE
    # SQLite：batch mode 重建表
    with op.batch_alter_table("recycle_decisions") as batch:
        batch.alter_column(
            "decided_at_utc",
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=False,
        )
    with op.batch_alter_table("recycle_jobs") as batch:
        batch.alter_column(
            "created_at_utc",
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=False,
        )
        batch.alter_column(
            "executed_at_utc",
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=True,
        )
    # ⚠️ 已有行按 +08 → UTC 修正（PostgreSQL 专用；SQLite 上是 no-op）
    # 原来写进去的 aware UTC 被按会话时区 +08 转成墙钟再丢掉时区，
    # 所以读出来要减 8 小时才是真正的 UTC。
    op.execute(
        "UPDATE recycle_decisions SET decided_at_utc = decided_at_utc - INTERVAL '8 hours' "
        "WHERE decided_at_utc IS NOT NULL"
    )
    op.execute(
        "UPDATE recycle_jobs SET created_at_utc = created_at_utc - INTERVAL '8 hours' "
        "WHERE created_at_utc IS NOT NULL"
    )
    op.execute(
        "UPDATE recycle_jobs SET executed_at_utc = executed_at_utc - INTERVAL '8 hours' "
        "WHERE executed_at_utc IS NOT NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("recycle_decisions") as batch:
        batch.alter_column(
            "decided_at_utc",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            existing_nullable=False,
        )
    with op.batch_alter_table("recycle_jobs") as batch:
        batch.alter_column(
            "created_at_utc",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            existing_nullable=False,
        )
        batch.alter_column(
            "executed_at_utc",
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            existing_nullable=True,
        )
