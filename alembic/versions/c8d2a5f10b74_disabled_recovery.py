"""record how an auto-disabled mission task gets re-enabled

Revision ID: c8d2a5f10b74
Revises: b3f5c8d10a27
Create Date: 2026-08-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8d2a5f10b74"
down_revision: str | None = "b3f5c8d10a27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 停用原因的**类别**，取值见 `domain.scheduler.DisabledRecovery`。
#:
#: 可空且刻意不给 server_default：NULL 的含义就是 `MANUAL`（「只有人能放它
#: 出来」），而那正是本列上线之前每一行的实际语义。给个默认值反而要多解释
#: 一次「没停用的行为什么也带着一个类别」。
_COLUMN = "disabled_recovery"

#: 升级那一刻已经因为航线不足挂着的行，一次性认领过来。
#:
#: ⚠️ **只有这一句可以看中文，而且只看历史数据。** 运行期的判据一个字都不比对
#: 文案（那正是加这一列的理由）；但升级之前落库的行只剩这句话可认，不认的话
#: 生产库里那条已经停用的 bot 任务升级完照样要用户手点一次「恢复」——而它正是
#: 这次改动要修的那一条。这句 SQL 两种方言都直接支持。
_LEGACY_REASON = "%空闲航线不足%"


def upgrade() -> None:
    # 可空列的 `ADD COLUMN` 两种方言都直接支持，不必走 `batch_alter_table`
    # 那条重建整张表的路（SQLite 上重建会连带丢掉本迁移看不见的东西）。
    op.add_column("mission_tasks", sa.Column(_COLUMN, sa.String(length=32), nullable=True))
    op.execute(
        sa.text(
            "UPDATE mission_tasks SET disabled_recovery = 'FREE_LINES' "
            "WHERE disabled_reason LIKE :reason"
        ).bindparams(reason=_LEGACY_REASON)
    )


def downgrade() -> None:
    op.drop_column("mission_tasks", _COLUMN)
