"""make the line-hold, reconcile-cooldown and bot-revisit thresholds configurable

Revision ID: d1a7f30c94e6
Revises: a7d4e91c05b3
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1a7f30c94e6"
#: 接在 `a7d4e91c05b3`（#167 的 `report_scan_hours`）后面，不是它俩共同的父节点
#: `c4e8b2f70a15`——两条都往 `military_attack_config` 加列，并排挂在同一个父节点上
#: 会变成两个 head，`alembic upgrade head` 直接报「Multiple head revisions」。
down_revision: str | None = "a7d4e91c05b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 2026-08-17「该不该可配置」审计的三个**运维旋钮**，一起搬到攻击配置页上。
#:
#: - `unknown_line_hold_minutes`：读不到飞行时间时这条航线占多久
#:   （`domain.report_wait.UNKNOWN_LINE_HOLD`，90 分钟）
#: - `reconcile_cooldown_minutes`：两次开工翻信箱之间至少隔多久
#:   （`domain.reconcile_cooldown.RECONCILE_COOLDOWN`，15 分钟）
#: - `bot_revisit_hours`：同一个 bot 坐标多久之内不重复打（24 小时）
#:
#: **三列全部可空，而且刻意不给 `server_default`。** NULL 的含义是「跟着代码里的
#: 默认值走」，这正是升级完成那一刻行为完全不变的保证；给了默认值，既有的那一行会
#: 被钉死在当时的取值上，日后调默认值它不跟。同 `c2a8f4d31e75`（盲拖屏数）。
#:
#: 可空列的 `ADD COLUMN` 两种方言（SQLite / PostgreSQL）都直接支持，
#: 不必走 `batch_alter_table` 那条重建整张表的路。
_TABLE = "military_attack_config"
_COLUMNS = (
    "unknown_line_hold_minutes",
    "reconcile_cooldown_minutes",
    "bot_revisit_hours",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.add_column(_TABLE, sa.Column(column, sa.Integer(), nullable=True))


def downgrade() -> None:
    for column in reversed(_COLUMNS):
        op.drop_column(_TABLE, column)
