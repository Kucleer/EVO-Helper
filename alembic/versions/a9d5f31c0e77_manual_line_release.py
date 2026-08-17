"""派遣记下人工释放航线的时刻

Revision ID: a9d5f31c0e77
Revises: a7f2c9d40b16
Create Date: 2026-08-17

新增 `attack_dispatches.line_released_at_utc`：用户在游戏里看过、确认这一发的
舰队已经回港之后，在调度台上按下「清理航线占用」写下的时刻。

**为什么不直接改 `line_free_at_utc`。** 那一列是**观测**——派出时读到的飞行
时长推算出来的返航时刻。把它改写成「现在」，这一发飞了多久就再也查不出来，
而飞行时长正是 `domain.report_wait.vet_flight_time` 那道下限赖以校准的样本
（生产库 209 发攻击里有 66 发落在 0–59 秒，靠的就是这批样本才认出来那是解析
截断的残骸）。两列分开之后，「舰队几点回来」与「人几点说它回来了」各说各的
话，谁也不吃掉谁。

**存量行一律 NULL**，含义是「没人手动放过手」，与这一列加进来之前的行为
完全一致：判据 `storage.repository._still_holding_a_line` 只在这一列为 NULL
时才继续看那两个钟。

⚠️ **时区语义按方言分岔。** `b6e0a4f21c98` 把 Postgres 上的业务时刻列统一成了
`TIMESTAMP WITH TIME ZONE`（SQLite 上那一步是真正的无操作，因为两种写法渲染出
的都是 `DATETIME`）。新列必须跟上：在 Postgres 上建成不带时区的话，
`storage.database.UTCDateTime` 写进去的 aware 值会被静默截成 naive，读回来
参与比较时不报错、只是安静地错。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9d5f31c0e77"
down_revision: str | None = "a7f2c9d40b16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(dialect_name: str) -> sa.DateTime:
    """这条库上该用的时刻类型。见模块头那段 ⚠️。

    方言名走参数而不是在函数里就地读 `op.get_bind()`：那样它只能在一次真正的
    `alembic upgrade` 中间被调用，于是「Postgres 上到底建成了哪一种」这件事
    在没有 Postgres 的机器上一条断言都写不出来。
    """
    return sa.DateTime(timezone=dialect_name != "sqlite")


def upgrade() -> None:
    # 可空列的 `ADD COLUMN` 在 SQLite 上是原生支持的单句 ALTER，不必走
    # `batch_alter_table` 把整张表连同外键重建一遍。
    op.add_column(
        "attack_dispatches",
        sa.Column("line_released_at_utc", _timestamp(op.get_bind().dialect.name), nullable=True),
    )


def downgrade() -> None:
    # 降级走 batch：老版本 SQLite 没有 `DROP COLUMN`，batch 会退回「重建表」。
    with op.batch_alter_table("attack_dispatches") as batch:
        batch.drop_column("line_released_at_utc")
