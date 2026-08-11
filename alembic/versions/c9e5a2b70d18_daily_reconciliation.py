"""add daily mailbox reconciliation counts

开工对账把「今天（UTC+0）信箱里数到几份本链路的攻击战报」记在这里，
供 `count_dispatches_since` 与库内派遣计数按 UTC 日取大。

这张表**不是派遣台账**：一行不代表一发派遣，对账也绝不往 `attack_dispatches`
里补行——凭空多一条派遣会让调度器以为一条航线被占着，并等一份永远不来的战报。

Revision ID: c9e5a2b70d18
Revises: a2f6c8d31b70
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9e5a2b70d18"
down_revision: str | None = "a2f6c8d31b70"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "daily_reconciliations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        # `YYYY-MM-DD`，UTC+0 的那一天。存字符串是为了能和 SQLite 的
        # `date(dispatched_at_utc)` 直接比。
        sa.Column("day_utc", sa.String(length=10), nullable=False),
        sa.Column("target_kind", sa.String(length=16), nullable=False),
        sa.Column("observed_reports", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("complete", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reconciled_at_utc", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("day_utc", "target_kind", name="uq_reconciliation_day_kind"),
    )
    op.create_index("ix_daily_reconciliations_day_utc", "daily_reconciliations", ["day_utc"])


def downgrade() -> None:
    op.drop_index("ix_daily_reconciliations_day_utc", table_name="daily_reconciliations")
    op.drop_table("daily_reconciliations")
