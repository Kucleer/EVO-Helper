"""派遣记下航线释放时刻与发次类型

Revision ID: d18b3f5c07ae
Revises: c7e4a1b95d62
Create Date: 2026-08-10

补的是航线记账的两个缺口，两列都加在 `attack_dispatches` 上。

**`line_free_at_utc`——派出之后的第二个钟。** `expected_report_at_utc` 是
「出发 + 飞行时长 × 1」，回答的是「战报出来没有」；航线要等舰队**飞回来**才释放。
一直拿前者判在飞数，调度器会在航线其实还占着时就去派，撞上游戏的
「同时派遣的舰队数量已达上限。」，白跑一整轮。倍数按发次分岔（攻击 ×2、
探路 ×1、侦察 ×2），算法在 `domain.report_wait.line_free_at`。

**存量行一律 NULL**：它们的 `flight_seconds` 本来就全是 NULL，无从回算。
NULL 不计入在飞数。

**`mission_kind`——攻击还是侦察。** 侦察占航线，但**不消耗**每天 32 次的攻击配额。
配额查询只按 `target_kind` 过滤，而侦察也是打向海盗的：不加这一列就没法把两者
分开，一轮 4 发侦察会静默吃掉 4 次攻击额度。

存量行一律 `ATTACK`：这一列加进来之前，侦察压根没有记录。**必须给
`server_default`**——老库里已有行，不给默认值 SQLite 会拒掉这条 ALTER
（上一条同类迁移 `f2b9d3c07a41` 踩过）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d18b3f5c07ae"
down_revision: str | None = "c7e4a1b95d62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attack_dispatches") as batch:
        batch.add_column(sa.Column("line_free_at_utc", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column(
                "mission_kind",
                sa.String(length=16),
                nullable=False,
                server_default="ATTACK",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("attack_dispatches") as batch:
        batch.drop_column("mission_kind")
        batch.drop_column("line_free_at_utc")
