"""记录舰队总数与逐行读数的不确定性

Revision ID: e1a7c4b2f905
Revises: d5a37c1e08b9
Create Date: 2026-08-09

总数与逐行明细是**两个独立来源**：总数来自战斗详情页的「单位」，
明细来自回放页的舰种列表。大舰队的数量显示成 `5.36K` 这样的四舍五入值，
逐行相加永远凑不出精确总数，所以总数必须单独存，不能由明细求和得到。

`uncertain` 标出那一行的数没有把握。攻击判断只看总数分档，
个别行不准不影响决策，但**必须让人看得出哪几行不能信**。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1a7c4b2f905"
down_revision: str | None = "d5a37c1e08b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("battle_reports") as batch:
        # 战斗详情页的「单位」总数，双方各一。可空：早先入库的战报没有这个数，
        # 补成 0 会让它看起来像「舰队为空」，那是假数据。
        batch.add_column(sa.Column("attacker_units", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("defender_units", sa.Integer(), nullable=True))
    with op.batch_alter_table("fleet_snapshots") as batch:
        # 既有行按「可信」处理：它们是人工核对过的那一份战报。
        batch.add_column(
            sa.Column("uncertain", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("fleet_snapshots") as batch:
        batch.drop_column("uncertain")
    with op.batch_alter_table("battle_reports") as batch:
        batch.drop_column("defender_units")
        batch.drop_column("attacker_units")
