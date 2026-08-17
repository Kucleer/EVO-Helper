"""make the routine report-scan floor configurable

Revision ID: a7d4e91c05b3
Revises: c4e8b2f70a15
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d4e91c05b3"
down_revision: str | None = "c4e8b2f70a15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 对账那一趟翻信箱最多往回读几个小时，从写死的 `MAX_REPORT_AGE`（6 小时）
#: 变成攻击配置页上的一个框。
#:
#: **可空，而且刻意不给 server_default**，同 `c2a8f4d31e75`：NULL 的含义是
#: 「跟着代码里的默认值走」，这正是升级完成那一刻行为完全不变的保证；给了默认值，
#: 既有的那一行会被钉死在当时的 6 上，日后调默认值它不跟。
#:
#: 可空列的 `ADD COLUMN` 两种方言（SQLite / PostgreSQL）都直接支持，
#: 不必走 `batch_alter_table` 那条重建整张表的路。
_TABLE = "military_attack_config"
_COLUMN = "report_scan_hours"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
