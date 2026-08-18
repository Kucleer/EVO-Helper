"""add the account-wide fleet line limit and the auto toggle log window

Revision ID: a4e7b1c93d20
Revises: e3f81b26a9d4
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4e7b1c93d20"
#: 接在 `e3f81b26a9d4`（军力时间池）后面。**保持单一 head**：这一条同样往
#: `military_attack_config` 加列，并排挂在同一个父节点上会变成两个 head，
#: `alembic upgrade head` 直接报「Multiple head revisions」。
down_revision: str | None = "e3f81b26a9d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "military_attack_config"

#: 全账号同时能在飞的舰队上限。用户口径（2026-08-18）：「我的总航线数是所有星球
#: 共享的，在启动加成道具情况下最高是到 9 条」。
#:
#: **它不是 `scheduler_config.fleet_line_limit`。** 那一列的含义早已降级成
#: 「任务没填航线数时用几条」（见 `storage.models.SchedulerConfigRow`），
#: 复用它等于让一个数同时表达两件互不相干的事。
#:
#: **可空，而且刻意不给 `server_default`**：NULL = 「跟着代码里的默认值走」
#: （`domain.scheduler.DEFAULT_ACCOUNT_LINE_LIMIT`）。给了默认值，既有那一行会被
#: 钉死在当时的取值上，日后调默认值它不跟。同 `e3f81b26a9d4` 与 `d1a7f30c94e6`。
_ACCOUNT_LINE_LIMIT = "account_line_limit"

#: 「自动停用 / 自动恢复」这一对日志的限流窗口（秒）。
#: 先例是 `record_unrecognised_screen` 的 120 秒。同样可空、同样不给默认值。
_AUTO_TOGGLE_LOG_SECONDS = "auto_toggle_log_seconds"


def upgrade() -> None:
    # 可空列的 `ADD COLUMN` 两种方言（SQLite / PostgreSQL）都直接支持，
    # 不必走 `batch_alter_table` 那条重建整张表的路。
    op.add_column(_TABLE, sa.Column(_ACCOUNT_LINE_LIMIT, sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column(_AUTO_TOGGLE_LOG_SECONDS, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _AUTO_TOGGLE_LOG_SECONDS)
    op.drop_column(_TABLE, _ACCOUNT_LINE_LIMIT)
