"""drop the military time pool knob

Revision ID: b1d9e47f2a03
Revises: a4e7b1c93d20
Create Date: 2026-08-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1d9e47f2a03"
#: 接在 `a4e7b1c93d20`（全账号航线上限 + 自动停用日志窗口）后面。**保持单一 head**：
#: 这一条同样动 `military_attack_config`，并排挂在同一个父节点上会变成两个 head，
#: `alembic upgrade head` 直接报「Multiple head revisions」。
down_revision: str | None = "a4e7b1c93d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "military_attack_config"

#: 「军力时间池」——**删掉它，因为它的存在本身就是一个错误设计的产物。**
#:
#: 它由 `e3f81b26a9d4`（PR #176）加进来，语义是选靶第 3 步「按军力读数时间倒序
#: 取前 N 个」。那一步是错的：军力榜是从强到弱扫的，所以「读数最新」系统性地
#: 等价于「军力最弱」（生产实测分段表在 `domain.target_order` 模块头第 3 步）。
#: 这个旋钮实际调的是「往最弱的那一头走多远」，而页面上写的是「用多新的数据」。
#:
#: 第 3 步现在换成按有效期**划一条线**（任务参数 `score_max_age_hours`）——划线
#: 不带选择偏差，也不再有「取前几个」这件事，所以这一列没有对应的语义可留。
#: 留着一列谁都不读的配置，只会让下一个人以为它还在起作用。
_COLUMN = "military_time_pool"


def upgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    # 加回来时仍然**可空、且不给 `server_default`**：这一列的 NULL 一直是
    # 「跟着代码里的默认值走」，给了默认值会把既有那一行钉死在当时的取值上。
    # 回滚回去的代码读的也是那一版的默认值（500）。
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))
