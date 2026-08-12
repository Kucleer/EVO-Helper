"""侦察报告入库：两张新表

Revision ID: d7a1c95e2f60
Revises: c9e5a2b70d18
Create Date: 2026-08-11

侦察报告此前只在内存里活一次：`PirateLoop.collect_scout_reports()` 读成
`PirateScoutReading` 交给 `_decide_and_attack()` 用一遍就丢，进程一退什么都不剩。
后果不只是查不到——海盗链路每一轮都当作没侦察过，于是同样四颗星球被来回重侦，
2026-08-11 当天 31 发派遣里有 25 发是重复侦察。

**不复用 `battle_reports`。** 那张表是攻击战报：`dispatch_id` 认领一发派遣、
`match_status` 记认领结果、`outcome` / `attacker_units` / `*_losses` 都是打完之后
才有的东西。侦察报告一样都没有，塞进去只会凭空多出一行「没认领上的战报」。

⚠️ **`scout_trigger_ships.count` 必须可空。** `NULL` 的含义是「这一格没读出来」，
和 0 是两回事：数量为 0 的格子在画面上只是一个孤零零的 `0`，实测最容易读空，
而三值判定（ATTACK / SKIP / UNREADABLE）整个建立在这个区分上。给它加
`NOT NULL DEFAULT 0` 就等于把「没看清」全部记成「这里是空的」。

去重口径与 `repository.has_report_at` 一致（目标 + 报告时间），落成
`uq_scout_reports_target_time`：活链路每一轮都会翻信箱里同样那几行，
没有这道约束一份报告会每趟复制一行。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7a1c95e2f60"
down_revision: str | None = "c9e5a2b70d18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scout_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reported_at_utc", sa.DateTime(), nullable=False),
        sa.Column("raw_time_text", sa.String(length=64), nullable=False),
        sa.Column("origin_galaxy", sa.Integer(), nullable=False),
        sa.Column("origin_system", sa.Integer(), nullable=False),
        sa.Column("origin_position", sa.Integer(), nullable=False),
        sa.Column("target_galaxy", sa.Integer(), nullable=False),
        sa.Column("target_system", sa.Integer(), nullable=False),
        sa.Column("target_position", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "target_galaxy",
            "target_system",
            "target_position",
            "reported_at_utc",
            name="uq_scout_reports_target_time",
        ),
    )
    op.create_index("ix_scout_reports_reported_at_utc", "scout_reports", ["reported_at_utc"])
    op.create_table(
        "scout_trigger_ships",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ship_type", sa.String(length=64), nullable=False),
        # ⚠️ 可空，且 **NULL ≠ 0**：NULL 是「这一格没读出来」。
        sa.Column("count", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["report_id"], ["scout_reports.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("report_id", "ship_type", name="uq_scout_trigger_report_ship"),
    )
    op.create_index(
        op.f("ix_scout_trigger_ships_report_id"), "scout_trigger_ships", ["report_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_scout_trigger_ships_report_id"), table_name="scout_trigger_ships")
    op.drop_table("scout_trigger_ships")
    op.drop_index("ix_scout_reports_reported_at_utc", table_name="scout_reports")
    op.drop_table("scout_reports")
