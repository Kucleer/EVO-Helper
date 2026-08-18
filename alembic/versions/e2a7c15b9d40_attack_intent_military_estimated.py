"""攻击意图记下「当时那个军力值是不是插出来的」

Revision ID: e2a7c15b9d40
Revises: c3f7a2b81d54
Create Date: 2026-08-18

`c3f7a2b81d54` 把「派出那一刻的军力读数」快照进了 `attack_intents`，但只抄了
数和时刻，没抄**那个数是怎么来的**。`bot_targets.military_score_estimated`
为真时，这一行从榜上读到的分数**破坏了降序、被判为不可信丢掉**，显示出来的数
是拿上下两个好邻居**插出来的中点**——不是读到的值。

来自 2026-08-15 的事故（记在 `tools/ranking_scan.py` 的注释里）：30 个 bot 的
军力飞到 10 万以上（最高 177 万），每一个除以 100 都精确落回正常区间——`17.73K`
读成 `1773K`，丢小数点。当时 `descending_breaks` 已经在报，但只打印、没据此丢，
于是 18 个错值进库、又经插值传染了 12 个。

**不是罕见情况**：2026-08-18 生产库里 3225 个有读数的 bot，估算的有 365 个
（11.3%）。日志上不标出来，等于把每九个里的一个插值当实读展示。

## 为什么必须快照，不能事后现取

`bot_targets.military_score_estimated` **会被反复重写和清零**：每轮采集整行覆盖、
`clear_pirate_position_bot_candidates` 清成 `False`、`forget_implausible_military_scores`
清成 `False`（2026-08-18 跑过两次，`3:386:7` 与 `4:336:11` 这两条的 estimated
就是这么从 `True` 变成 `False` 的）。事后现取，今天标着「估算」的记录明天会
自己变成「实读」——**页面会说假话，而且一声不响**。

⚠️ **可空，且不给 `server_default`。** 这一列上 `False` 的含义是「这个数是实读
的」，而存量意图**根本不知道**当时那个数是怎么来的。默认成 `False` 等于让所有
历史行冒充实读；NULL 才是实话：「当时没记这件事」。页面据此既不标「(估算)」、
也不声称实读。

⚠️ 它**不是脏数据探测器**。2026-08-18 的反例：`8:452:16` 的 262,899 标着
estimated=True，而 `3:398:19` 的 262,000 一样脏却是 `False`——后者是榜单名次 4
的真人玩家被当成 bot 收进来，262,000 是真读到的，降序判据挑不出它。
**这一列只回答「这个数是插出来的吗」，不回答「这个数对不对」。**
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e2a7c15b9d40"
#: 接在 `c3f7a2b81d54`（攻击意图的军力快照）后面。**保持单一 head**：并排挂在
#: 同一个父节点上会变成两个 head，`alembic upgrade head` 直接报
#: 「Multiple head revisions」，而生产的升级机制就是启动时跑它。
down_revision: str | None = "c3f7a2b81d54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "attack_intents"
_ESTIMATED = "target_military_score_estimated"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        # nullable=True 且**不给 server_default**：理由见模块开头那段警告。
        batch.add_column(sa.Column(_ESTIMATED, sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_ESTIMATED)
