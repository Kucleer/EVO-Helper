"""军力榜盲滚改按「行」配：新增 blind_scroll_rows

Revision ID: b8e1c4a72f05
Revises: a3c81f5d2b64
Create Date: 2026-08-22

盲滚那一段从慢拖换成滚轮之后，口径也跟着从「屏」换成「**行**」：滚轮那一段量得到
的就是行（实测 2026-08-22），而「一屏是多少行」本身是个会飘的换算，配置里再经它一
道等于把误差腌进用户填的数里。

所以加一列 `military_attack_config.blind_scroll_rows`。

⚠️ **可空，而且刻意不给 `server_default`。** NULL 的含义是「跟着代码里的默认值
`game.ranking_ui.BLIND_SCROLL_ROWS`(700) 走」——这正是升级完成那一刻行为完全不变的
保证。形状照上一列 `blind_scrolls`（`c2a8f4d31e75`）：给了默认值就分不开「没配」和
「恰好配成了当前默认」，日后把 700 调成别的数时，存量那一行会被钉死在 700 上，而它
表达的其实是「跟着默认走」。

⚠️ **`blind_scrolls`（屏）那一列保留不删，这条迁移一个字都不碰它。** 它是这次改动
的一键回滚：`blind_scroll_rows` 置空即退回慢拖那条路，不需要改代码、不需要再来一条
迁移。顺手清理掉它，回滚就变成「改代码 + 重新发版」。

⚠️ **不设上界，也不据 `FIRST_BOT_RANK`(587) 做任何判断。** 用户口径（2026-08-22）：
榜上那个「bot 起点」是**玩家改名伪装**出来的（判据只看名字前缀 `bot_`，改名的真人
一样命中），真 bot 区在更后面，所以 700 行并不越界。拿一个被伪装污染的边界报警，比
不报警更坏。0 是合法取值（「一格都不拨」是最保守取值）。

同 `c2a8f4d31e75`：可空列的 `ADD COLUMN` 两种方言（SQLite / PostgreSQL）都直接支持，
不必走 `batch_alter_table` 那条重建整张表的路。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8e1c4a72f05"
#: 接在 `a3c81f5d2b64`（配着几条航线 + 挂机心跳）后面，也就是当时的 head。
#: **保持单一 head**：并排挂在同一个父节点上会变成两个 head，`alembic upgrade head`
#: 直接报「Multiple head revisions」，而生产的升级机制就是启动时跑它
#: （`web.runtime._upgrade_database`）——症状是用户重启 bat 之后控制台起不来。
down_revision: str | None = "a3c81f5d2b64"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "military_attack_config"
_COLUMN = "blind_scroll_rows"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
