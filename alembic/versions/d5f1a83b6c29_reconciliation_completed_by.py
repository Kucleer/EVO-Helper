"""对账是谁翻完的

Revision ID: d5f1a83b6c29
Revises: c4e7b2a91d68
Create Date: 2026-09-13

回收报告读哪一条路由，判据是「信箱回读任务上一次**自己**翻完是什么时候」。
而 `reconciled_at_utc` 是空闲趟与开工兜底趟**共享**的：兜底趟一直在翻时那个时刻
永远新鲜，路由会一直以为空闲趟活着 —— 于是两边都不读回收报告。

⚠️ **不能拿现有的 `complete` 列顶替**：那一列说的是「当日份数数全了没」，
和「是谁翻的」不是一件事。

⚠️ **旧行留 NULL，不回填。** NULL 的含义是「这一行记的时候还没有来源这个概念」，
而回填成 `'round'` 是在编造一个我们并不知道的事实 —— 何况路由只认 `'mail'`，
把旧行填成 `'round'` 对判据没有任何影响，只是多一份假数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5f1a83b6c29"
down_revision: str | Sequence[str] | None = "c4e7b2a91d68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("daily_reconciliations") as batch:
        batch.add_column(sa.Column("completed_by", sa.String(length=8), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("daily_reconciliations") as batch:
        batch.drop_column("completed_by")
