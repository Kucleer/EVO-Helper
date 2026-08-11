"""daily_reconciliations: 把当天的攻击状态一并存下来，供快速回读

用户口径（2026-08-11）：「每天的海盗次数（状态）也可以存库，这样也可以快速回读。」

这张表原先**只有信箱那一侧的观测数**（`observed_reports`），答不上两个问题：

- 「今天一共算打了几发」——那个数要现去跑 `count_dispatches_since`；
- 「还有几发在等战报」——库里压根没有。

于是重启之后想知道「今日 X/32、几发在飞」，除了再翻一趟信箱没有别的办法。

⚠️ 这三列**仍然不是派遣台账**：一行不代表一发派遣，对账也绝不往
`attack_dispatches` 里补行——凭空多一条派遣会让调度器以为一条航线被占着、
并等一份永远不来的战报。

存量行补 0 而不是回填：回填要按今天的判据去重算历史某一天，而那几天的
`attack_dispatches` 早就被 `MAX_REPORT_AGE` 判过一轮，算出来的数没有意义。
下一次对账会把当天这三列写成真话，更早的日子只作历史观测数留着。

Revision ID: f4c2e91a7b63
Revises: d7a1c95e2f60
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4c2e91a7b63"
down_revision: str | None = "d7a1c95e2f60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("daily_reconciliations") as batch:
        batch.add_column(
            sa.Column("dispatched_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("attacks_used", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("awaiting_reports", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("daily_reconciliations") as batch:
        batch.drop_column("awaiting_reports")
        batch.drop_column("attacks_used")
        batch.drop_column("dispatched_count")
