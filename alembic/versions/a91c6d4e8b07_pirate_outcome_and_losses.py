"""战报记下胜负与战损总数

Revision ID: a91c6d4e8b07
Revises: f2b9d3c07a41
Create Date: 2026-08-09

海盗战报**只记胜负与战损总数**（用户口径 2026-08-09，为省性能）：
逐舰种明细要进回放页、读两列名称与数量、还要反复重拍到合计对上，
一份报告多花两三秒，而海盗全是同一个预设打的，明细没有分析价值。

三个字段都可空，而且**必须可空**：这之前入库的战报没有读过胜负与战损，
给 `outcome` 补个值等于凭空造出战果，给战损补 0 等于凭空造出「零损失」。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a91c6d4e8b07"
down_revision: str | None = "f2b9d3c07a41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("battle_reports") as batch:
        # 存游戏画面上的原文 `VICTORY` / `FAIL`，不翻译：界面显示中文是渲染层的事，
        # 库里存读到的那个词，才能事后拿截图对着核。
        batch.add_column(sa.Column("outcome", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("attacker_losses", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("defender_losses", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("battle_reports") as batch:
        batch.drop_column("defender_losses")
        batch.drop_column("attacker_losses")
        batch.drop_column("outcome")
