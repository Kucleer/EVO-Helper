"""永久拉黑一个坐标：模仿 bot 命名的玩家不再进选靶、也不再进军力榜

Revision ID: a7d3e91c05b2
Revises: c7f2a91d4e08
Create Date: 2026-08-27

用户口径（2026-08-27，逐字）：

    「4:268:5 这个坐标做特殊处理，永久移出军力榜，做黑名单
     1.这个坐标是玩家，他的 ID 是模仿 bot 命名
     2.因为军力差距过大，所以我们无法发起攻击」

实测代价：2026-08-27 一天里 4 系起了 17 轮，**每一轮都挑中他**（军力 10580、离主星
又近，排序上永远靠前），每一轮都在派遣面板上撞一个我们认不出的弹窗，整轮作废。
他一个人吃掉了 4 系一整天（07:00 之后一发没派出去）。

## ⚠️ 为什么是「时刻 + 理由」而不是一个布尔

`bot_targets` 上已经有两个排除概念（`protection_seen_at_utc`、`unreadable_seen_at_utc`），
都是「时刻 + 旋钮」：时刻是事实，排除多久是策略。**这一列没有旋钮**，因为它记的事实
本身就是永久的——等多久他都还是玩家。

理由那一列不是装饰。拉黑是永久的，三个月后翻到这一行，「他是模仿 bot 命名的玩家」
与「那阵子扫描坏了误判的」是完全不同的两件事，而**只有一个时刻的话，两者长得一模
一样**。没有理由就没人敢把它放回来，于是错拉的黑永远拉着。

## 两列都可空、都不给 server_default

NULL = 「没拉黑」，不是某个具体时刻。给个默认时刻会把全库六千多行一次性拉黑。
同 `d4b6e0f19c73` 那两列的取值约定。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d3e91c05b2"
down_revision: str | Sequence[str] | None = "c7f2a91d4e08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "bot_targets"
_AT = "blacklisted_at_utc"
_REASON = "blacklist_reason"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_AT, sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column(_REASON, sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_REASON)
        batch.drop_column(_AT)
