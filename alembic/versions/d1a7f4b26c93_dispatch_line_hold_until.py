"""航线按**距离**兜底占到什么时候，以及派出那一刻的舰速

Revision ID: d1a7f4b26c93
Revises: c4f8a2e51b07
Create Date: 2026-08-19

两列一起加，因为它们修的是同一次故障的两半。

## `fleet_speed_raw`：派出这一刻简报页上的舰队速度原文

用户口径（2026-08-19）：**「每个球的速度都会有点不一样的」**。所以
`domain.flight_time.SECONDS_PER_ROOT_UNIT` 那个全局常数结构上只可能对一颗星球
成立——生产库回测（跨银河那一档）反解 `单程秒 = 2 + k·√D`：

    4:277:15  n=56  k = 26.5165
    9:250:8   n=19  k = 26.3327

系数改成**按出发星球从历史实测里学**（`domain.flight_estimate.
fit_seconds_per_root_unit`），而这一列是那份学习的**作废信号**：编组一换，
屏幕上这个数第一发就变了，旧样本立刻不算数。`preset_name` 与 `preset_signature`
都抓不住那次变化——2026-08-17 那天 13 发慢了 26% 就是这么错过去的。

⚠️ **它不参与任何算术。** 按速度比缩放系数是错的：9:250:8 的 k ÷ 4:277:15 的 k
= 0.9931，而速度比 14.520/14.720 = 0.98641，差 0.7%。存成字符串正是为了让下一个
人写不出 `14.520 / speed` 那个乘法。

## `line_hold_until_utc`：见下

在这一列之前，飞行时长一个来源都定不下来的那些派遣，一律按
`domain.report_wait.UNKNOWN_LINE_HOLD`（90 分钟）这个**与目标无关的常数**占航线。
而真实往返强烈依赖距离：同恒星系内十几分钟，跨银河两小时出头。

实机 2026-08-19，从 9:250:8 打三个跨银河 bot，单程 3726 秒、往返 124.2 分钟：

    派出后第 90 分钟  →  航线被判为空出来了
    实际第 124 分钟   →  舰队才回港
    中间那 34 分钟    →  调度器与首页都以为有空闲航线，而实际没有

用户看到的「星球 2 在等航线」就是这么来的。

这一列存的是 `派出时刻 + 距离公式算出来的往返 × 1.3`（系数见
`domain.flight_estimate.LINE_HOLD_SAFETY_FACTOR`）。

⚠️ **它不取代那个常数，而是与它取大**（判据在
`storage.repository._still_holding_a_line`）：攻击配置页上那个
`unknown_line_hold_minutes` 旋钮照旧在查询时生效，用户填了就听用户的。
两者取大，这一档只会占得更久、绝不会更短——低估会让调度器以为有航线、派出去撞
游戏的「同时派遣的舰队数量已达上限。」，白跑一整轮，比高估贵得多。

⚠️ **可空，而且刻意不给 `server_default`。** 存量行没有这个估算，NULL 就是
「算不出来，走那个常数」，与 `flight_source` 那一列同一条理由：不许给历史行
编一个看起来像算过的值。同一个恒星系内那一档也照旧写 NULL——距离公式在那一档是
已知不准的（`1162` 反推只有约 520），而它算出来的往返本来就压在 90 分钟底下。

可空列的 `ADD COLUMN` 两种方言（SQLite / PostgreSQL）都直接支持，不必走
`batch_alter_table` 那条重建整张表的路。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1a7f4b26c93"
#: 接在 `c4f8a2e51b07`（飞行时长来源）后面。**保持单一 head**：并排挂在同一个
#: 父节点上会变成两个 head，`alembic upgrade head` 直接报「Multiple head
#: revisions」，而生产的升级机制就是启动时跑它（`web.runtime._upgrade_database`）。
#: 合并时若发现别的分支也挂在 `c4f8a2e51b07` 上，**后进的那条改自己的
#: `down_revision`**，别去动已经合进去的。
down_revision: str | None = "c4f8a2e51b07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "attack_dispatches"
_HOLD_COLUMN = "line_hold_until_utc"
_SPEED_COLUMN = "fleet_speed_raw"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_HOLD_COLUMN, sa.DateTime(timezone=True), nullable=True))
    op.add_column(_TABLE, sa.Column(_SPEED_COLUMN, sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _SPEED_COLUMN)
    op.drop_column(_TABLE, _HOLD_COLUMN)
