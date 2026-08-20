"""记下每轮开始时配着几条航线，并给挂机心跳开一张表

Revision ID: a3c81f5d2b64
Revises: d4b6e0f19c73
Create Date: 2026-08-20

用户口径（2026-08-20）**反转了他自己 08-17 定的那一条**：航线利用率的分母从
「任务实际运行时间 × 航线数」换成「**一天的总时长 × 航线数**」，并新增一个
「挂机运行时长」。这条迁移只落两样数据，判据在 `domain.overview` / `domain.uptime`。

## 1. `mission_runs.configured_lines`（可空）

新分母里线数是**唯一**的乘数（旧分母里它只是其中一个），所以「那一天配着几条」
错多少，利用率就错多少倍。而 `mission_runs` 从来没记过这个数。

⚠️ **可空，而且一个 `server_default` 都不给。** NULL 的意思是「不知道」：

- 填 0 会让那些天的分母变成 0，页面上整段显示成「—」，把那天真打出去的活抹掉；
- 回填「此刻配着几条」是拿现在的配置去顶历史——用户 2026-08-20 当天把航线从 4 条
  加到 9 条，按 9 条去算 08-15（当时 4 条）会把那天低估到 44%，**而页面上一点
  异样都看不出来**。

NULL 的那些天改用「当天观测到的最大并发在飞数」当**下界**（用户选定的方案 C），
方向是 **线数取下界 ⇒ 分母偏小 ⇒ 利用率取上界**，页面上给那种格子标「≤」。

## 2. `scheduler_uptime_segments`（新表）

「挂机运行时长」现在**取不到**：`state_events` 全表只有 1 行、写它的路径早删了
（`web.persistent_service` 里 `_event` 那段注释）；而拿 `mission_runs` 的轮次覆盖
冒充挂机时长会说假话——实测 2026-08-20 近 24 小时里，轮次覆盖 17.7h、跨度 23.8h，
空隙合计 6.0h，其中 11:45→12:25 那 41 分钟**调度器是开着的**（扫描间隔挡住
RANKING、`waiting_for_a_line` 压住 BOT）。

所以落**心跳**：一段一行，每分钟把 `last_beat_at_utc` 往前推。

⚠️ **刻意没有「结束时刻」这一列。** 进程被杀时不会有人来写它，而「最后一拍」
天然就是这一段的右端——挂机时长因此不会在崩溃后一直涨。

⚠️ **这张表补不回历史。** 心跳之前的那些天必须在页面上显示「—」而不是 0：
显示 0 等于说「那天没开机」，那是假话。

可空列的 `ADD COLUMN` 与 `CREATE TABLE` 两种方言（SQLite / PostgreSQL）都直接
支持，不必走 `batch_alter_table` 那条重建整张表的路。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3c81f5d2b64"
#: 接在 `d4b6e0f19c73`（面板名读不出）后面。**保持单一 head**：并排挂在同一个
#: 父节点上会变成两个 head，`alembic upgrade head` 直接报「Multiple head revisions」，
#: 而生产的升级机制就是启动时跑它（`web.runtime._upgrade_database`）。
down_revision: str | None = "d4b6e0f19c73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS = "mission_runs"
_LINES = "configured_lines"
_UPTIME = "scheduler_uptime_segments"


def upgrade() -> None:
    op.add_column(_RUNS, sa.Column(_LINES, sa.Integer(), nullable=True))
    op.create_table(
        _UPTIME,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        # ⚠️ 两列都必须带时区：`UTCDateTime` 的 impl 是 `DateTime(timezone=True)`，
        # 写成不带时区的话 Postgres 上 tzinfo 会被**静默截掉**，读回来是 naive，
        # 而这个仓所有的判据都建立在「读出来是 aware 的 UTC」上。
        sa.Column("started_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_beat_at_utc", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(f"ix_{_UPTIME}_started_at_utc", _UPTIME, ["started_at_utc"])
    op.create_index(f"ix_{_UPTIME}_last_beat_at_utc", _UPTIME, ["last_beat_at_utc"])


def downgrade() -> None:
    op.drop_index(f"ix_{_UPTIME}_last_beat_at_utc", table_name=_UPTIME)
    op.drop_index(f"ix_{_UPTIME}_started_at_utc", table_name=_UPTIME)
    op.drop_table(_UPTIME)
    op.drop_column(_RUNS, _LINES)
