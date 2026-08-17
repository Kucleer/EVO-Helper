"""give each mission task an optional start/stop moment

Revision ID: b3f5c8d10a27
Revises: a9d5f31c0e77
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3f5c8d10a27"
down_revision: str | None = "a9d5f31c0e77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 两列都**可空**，而且刻意不给 server_default。
#:
#: 可空是判据的一部分而不是省事：NULL 的含义是「这一端不限」，
#: 见 `domain.scheduler.within_schedule_window`。给了默认值，升级完成的那一刻
#: 每个既有任务都会凭空多出一个窗口——而那正是「两列都为空 = 行为完全不变」
#: 这条承诺的反面。
#:
#: `timezone=True` 不能省：Postgres 上没有它就是 `TIMESTAMP WITHOUT TIME ZONE`，
#: tzinfo 被静默截掉（见 `storage.database.UTCDateTime` 的注释）。SQLite 上
#: 两者都建成 `DATETIME`，写不写没区别，所以这一句是为生产库写的。
_COLUMNS = ("enabled_from_utc", "enabled_until_utc")


def upgrade() -> None:
    # 可空列的 `ADD COLUMN` 两种方言都直接支持，不必走 `batch_alter_table`
    # 那条重建整张表的路（SQLite 上重建会连带丢掉本迁移看不见的东西）。
    for name in _COLUMNS:
        op.add_column("mission_tasks", sa.Column(name, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("mission_tasks", name)
