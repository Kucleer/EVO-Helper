"""回收启用时刻列

Revision ID: a7c3e5d9f2b1
Revises: f6b9d4e2a1c3
Create Date: 2026-09-10

⚠️ Bug 1 修复：决策扫描的 `since` 原来从 `last_decided` 推，产生棘轮效应 ——
一发攻击约 1 小时后才释放，而那时 `since` 已被后来的决策推到它的派遣时刻之后，
它永远出局。生产实测：22 发攻击只产生 1 条决策。

改成固定下界：滑块从 0 调到非 0 的那一刻写一次，之后再调档位不更新。

⚠️ **先把已有数据的启用时刻补上**，否则放开条件会一次性放出 3919 条历史派遣。
取那条唯一决策的时刻附近的一个保守值。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c3e5d9f2b1"
down_revision: str | Sequence[str] | None = "f6b9d4e2a1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("military_attack_config") as batch:
        batch.add_column(
            sa.Column("recycle_enabled_at_utc", sa.DateTime(timezone=True), nullable=True)
        )
    # ⚠️ 已有数据：如果 rate_tenths 已经非 0，把启用时刻补成一个保守值。
    # 取 2026-09-10 13:00 UTC（那条唯一决策 21:13:30 +08 = 13:13:30 UTC 附近）。
    # 别取 0 —— 那会把 3919 条历史派遣一次性放出来。
    op.execute(
        "UPDATE military_attack_config SET recycle_enabled_at_utc = '2026-09-10 13:00:00+00' "
        "WHERE recycle_rate_tenths IS NOT NULL AND recycle_rate_tenths > 0 "
        "AND recycle_enabled_at_utc IS NULL"
    )


def downgrade() -> None:
    with op.batch_alter_table("military_attack_config") as batch:
        batch.drop_column("recycle_enabled_at_utc")
