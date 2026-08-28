"""连崩停用改成「冷却之后自己回来」：两列退避状态落库

Revision ID: f2c04b8ae153
Revises: a7d3e91c05b2
Create Date: 2026-08-28

## 在修什么

2026-08-28 00:01 生产实况：游戏窗口没了，六个任务（1 个 RANKING + 5 个 BOT）每轮
起来约 1 秒就死、`exit=1`。环境故障豁免一路吃到 6/6（≈半小时），01:02:49 用尽，
连续失败计满 `MAX_CONSECUTIVE_FAILURES=3`，六个任务全部自动停用、恢复方式
`MANUAL`——**然后就没有然后了**。它们一直关到早上用户手动打开（bot 约 07:2x、
军力榜 09:32），而环境早就自己好了；军力榜多关了两个多小时才被发现。

用户口径（2026-08-28，逐字）：「我的预期是这些任务都应该自动重启才对」。

原先给 `MANUAL` 的理由是「自动恢复会让调度循环退回那个满速空转的重启循环」。
这个担心是对的，但结论下猛了：**防空转靠的是冷却，不是永不恢复。** 新的一档
（`DisabledRecovery.BACKOFF`）按 15 分 → 30 分 → 1 小时封顶重试，不设终点。
按这条曲线，那一夜六个任务会在 **01:17** 自己回来。

## 为什么状态非落库不可

退避是**时间驱动**的，而调度器进程会重启（改配置、装新版本都会）。挂在内存里的
闹钟一重启就没了，任务于是又变回「关了就再也不开」——正是这次要修的那个样子。
`FREE_LINES` 那一档可以每 tick 现算（「此刻有没有空闲航线」重启后照样算得出来），
这一档算不出来：「上次是什么时候停的、这是第几轮」除了库里没有第二个地方记着。

## 两列的取值约定

- `retry_after_utc`（可空，**不给** `server_default`）
  到了这个时刻就自动放回来。**NULL = 「没有在等冷却」**，不是某个具体时刻：
  没停用的行、被别的方式停用的行、以及本列上线之前的所有历史行都是 NULL，
  判据把 NULL 一律读成「不该恢复」。给它一个默认时刻等于把全库每一行都摆成
  「随时可以自动打开」——包括用户自己关掉的那四个任务，那是这次唯一不能出的错。

- `backoff_rounds`（非空，`server_default="0"`）
  连着被自动停用了几轮，退避间隔按它查曲线。它是个**计数**，「不知道」这个状态
  对它没有意义，所以不留 NULL：留了的话每一处读它的地方都要写一遍 `or 0`，
  漏一处就是一次静默的「退避不递增」。非空列加到既有表上必须有 `server_default`，
  否则老行没法填——取 0，语义正是「当前这一串还没开始」，与全新建的行一致。

⚠️ 与 `c8d2a5f10b74`（`disabled_recovery`）**刻意不同**：那一条顺手认领了升级前
就挂着「空闲航线不足」的历史行。这一条**一行历史数据都不动**——升级之前被
`MANUAL` 停用的任务保持要人工，因为库里已经分不清它当初是「连崩到上限」还是
「参数填错」（两者当初写的都是 `MANUAL`），而认错的代价是把一条配置填错的链路
每小时白起一次、永远不停。新的一次停用才走新曲线。

## 为什么用 `batch_alter_table`

`downgrade` 要 `drop_column`，而 SQLite 上删列要重建整张表。照 `a7d3e91c05b2`
的形状走 batch 模式，两个方向对称。⚠️ 生产是 PostgreSQL、本地跑 SQLite，
本条只用了两种方言都直接支持的 `ADD COLUMN` / `DROP COLUMN`，没有方言分岔。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2c04b8ae153"
down_revision: str | Sequence[str] | None = "a7d3e91c05b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "mission_tasks"
_RETRY_AFTER = "retry_after_utc"
_ROUNDS = "backoff_rounds"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_RETRY_AFTER, sa.DateTime(timezone=True), nullable=True))
        batch.add_column(
            sa.Column(_ROUNDS, sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_ROUNDS)
        batch.drop_column(_RETRY_AFTER)
