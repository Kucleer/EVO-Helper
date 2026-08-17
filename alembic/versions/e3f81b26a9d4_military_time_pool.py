"""add the configurable military time pool

Revision ID: e3f81b26a9d4
Revises: d1a7f30c94e6
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e3f81b26a9d4"
#: 接在 `d1a7f30c94e6`（#170 的三个行为旋钮）后面。**保持单一 head**：
#: 两条都往 `military_attack_config` 加列，并排挂在同一个父节点上会变成两个 head，
#: `alembic upgrade head` 直接报「Multiple head revisions」。
down_revision: str | None = "d1a7f30c94e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 军力时间池：按军力读数时间倒序取前几个进池，军力截断（任务参数 `top_n`）
#: 在这一池**之内**生效。用户口径（2026-08-18）：默认 500。
#:
#: **可空，而且刻意不给 `server_default`。** NULL 的含义是「跟着代码里的默认值走」
#: （`domain.target_order.DEFAULT_TIME_POOL`），这正是升级完成那一刻行为完全不变的
#: 保证；给了默认值，既有的那一行会被钉死在当时的取值上，日后调默认值它不跟。
#: 同 `c2a8f4d31e75`（盲拖屏数）与 `d1a7f30c94e6`（三个行为旋钮）。
#:
#: 可空列的 `ADD COLUMN` 两种方言（SQLite / PostgreSQL）都直接支持，
#: 不必走 `batch_alter_table` 那条重建整张表的路。
_TABLE = "military_attack_config"
_COLUMN = "military_time_pool"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
