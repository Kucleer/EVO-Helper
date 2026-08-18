"""把「撞上保护期」按目标记下来，并给「排除多久」开一个旋钮

Revision ID: b7e4d0c93a15
Revises: e2a7c15b9d40
Create Date: 2026-08-18

实机 2026-08-18 20:29 那一轮：四个目标当场全部确认在保护期内、11.5 分钟一发没派；
20:41 结算完，**一秒之后的下一轮又把同样的四个挑了出来**，如此往复直到 8 小时
自然过去。成因是「在保护期内」这件事**没有落库**——`bot_targets` 上没有任何记录
它的列，164 条 `[拦下]` 全是 `system_log` 里的纯文本，选靶查不到。

游戏的保护期是 8 小时，**任何人打过都会触发**，而且**只能撞上了才知道**
（`game.pirate_ui.DIALOG_NO_MISSION`）。代价：每个目标每轮约 2.9 分钟鼠标时间
（导航 + 开面板 + 撞弹窗 + 退出），一轮四个就是 11.5 分钟，而这台机器一天的鼠标
时间本来只有 56% 在干活。

两列，两张表：

- `bot_targets.protection_seen_at_utc`：**在什么时刻撞上的**（事实）。
- `military_attack_config.protection_exclusion_hours`：**撞上之后排除多久**（策略）。

⚠️ **这两件事必须分开存。** 「保护期 8 小时」是游戏规则；「排除 8 小时」是我们
自己的取舍——我们只知道撞上的时刻 T，不知道保护期何时开始，所以按 T+8h 排除必然
过度。过度排除只是少打几个（候选池 3000+），排除不足是每轮白烧鼠标时间，代价
不对称，故默认宁可过度。把「排除到什么时候」直接算好存进 `bot_targets`，等于把
当时那份策略腌进历史数据，日后调旋钮旧行不跟。

⚠️ **两列都可空，而且刻意不给 `server_default`。**

- `protection_seen_at_utc` 的 NULL 是「从没撞上过」，不是某个时刻。给个默认时刻
  会让全库存量目标一次性被排除掉。
- `protection_exclusion_hours` 的 NULL 是「跟着代码里的默认值走」（同
  `d1a7f30c94e6` 的三个旋钮）。写死当时的取值，日后调默认值它不跟。

可空列的 `ADD COLUMN` 两种方言（SQLite / PostgreSQL）都直接支持，不必走
`batch_alter_table` 那条重建整张表的路。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e4d0c93a15"
#: 接在 `e2a7c15b9d40`（攻击意图的「军力值是不是插出来的」）后面。**保持单一
#: head**：并排挂在同一个父节点上会变成两个 head，`alembic upgrade head` 直接报
#: 「Multiple head revisions」，而生产的升级机制就是启动时跑它。
down_revision: str | None = "e2a7c15b9d40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGETS = "bot_targets"
_SEEN_AT = "protection_seen_at_utc"
_CONFIG = "military_attack_config"
_EXCLUSION = "protection_exclusion_hours"


def upgrade() -> None:
    op.add_column(_TARGETS, sa.Column(_SEEN_AT, sa.DateTime(timezone=True), nullable=True))
    op.add_column(_CONFIG, sa.Column(_EXCLUSION, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_CONFIG, _EXCLUSION)
    op.drop_column(_TARGETS, _SEEN_AT)
