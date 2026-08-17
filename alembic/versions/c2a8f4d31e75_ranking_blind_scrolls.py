"""make the ranking blind-scroll count configurable

Revision ID: c2a8f4d31e75
Revises: c8d2a5f10b74
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c2a8f4d31e75"
down_revision: str | None = "c8d2a5f10b74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 军力榜采集「开榜后先盲拖几屏」，从写死的 40 变成攻击配置页上的一个框。
#:
#: **可空，而且刻意不给 server_default。** NULL 的含义是「跟着代码里的默认值
#: 走」（`game.ranking_ui.BLIND_SCROLLS`），这正是升级完成那一刻行为完全不变的
#: 保证；给了默认值，既有的那一行会被钉死在当时的 40 上，日后调默认值它不跟。
#:
#: 同 `b3f5c8d10a27`：可空列的 `ADD COLUMN` 两种方言（SQLite / PostgreSQL）都
#: 直接支持，不必走 `batch_alter_table` 那条重建整张表的路。
_TABLE = "military_attack_config"
_COLUMN = "blind_scrolls"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
