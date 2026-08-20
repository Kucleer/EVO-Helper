"""把「面板名读不出」按目标记下来，并给「排除多久」开一个旋钮

Revision ID: d4b6e0f19c73
Revises: 61eb261c5a09
Create Date: 2026-08-20

生产库 + `system_log` 实测（2026-08-20，近 24 小时）：「不是 bot（面板名 None）」
出现 **40 次、只涉及 3 个坐标**，而「不是 bot」但真读出了名字的 **0 次**——也就是
这个判据 100% 是在报识别失败，从来没真的认出过一个「非 bot」。这 3 个坐标（军力
39,030 / 20,960 / 20,630）历史上成功派出 0 次。

死循环怎么闭合的：军力高 → 排在候选池最前 → 站过去读不出 → 判「不是 bot」跳过 →
这一轮 0 发 → `came_back_empty` 让 `waiting_for_a_line` 把那颗球压到下一条航线空出
（实测一次 117 分钟）→ **失败没有留下任何记录**，下一轮候选池一个字没变，又挑中
同一个。近 24 小时 65 轮里 16 轮空手而归（25%）。

这条迁移只修「失败不留记录」那一环，**不碰面板名为什么读不出**（识别层，根因未知）。
形状照抄 `b7e4d0c93a15`（保护期那一条）——它解决的是一模一样的问题。

三列，两张表：

- `bot_targets.unreadable_seen_at_utc`：**什么时候读不出的**（事实）。
- `bot_targets.unreadable_attempts`：**连续第几次读不出**（事实，读通即归零）。
- `military_attack_config.unreadable_exclusion_hours`：**之后排除多久**（策略）。

⚠️ **两个时刻/策略必须分开存**，同 `b7e4d0c93a15`：把「排除到什么时候」算好存进
`bot_targets`，等于把当时那份策略腌进历史数据，日后调旋钮旧行不跟。

⚠️ **默认值三列各不相同，都是照实说：**

- `unreadable_seen_at_utc` 可空、**不给** `server_default`：NULL 是「从没读不出过」，
  不是某个时刻。给个默认时刻会让全库存量目标一次性被排除掉。
- `unreadable_attempts` 非空、`server_default="0"`：0 对存量行是**真话**（还没失败
  过）。这里和上一列的处理不同不是疏漏——那一列没有一个诚实的默认时刻可给。
- `unreadable_exclusion_hours` 可空、**不给** `server_default`：NULL 是「跟着代码里
  的默认值走」（同 `d1a7f30c94e6` 的三个旋钮）。写死当时的取值，日后调默认值它不跟。

可空列与「带 `server_default` 的非空列」的 `ADD COLUMN` 两种方言（SQLite /
PostgreSQL）都直接支持，不必走 `batch_alter_table` 那条重建整张表的路。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4b6e0f19c73"
#: 接在 `61eb261c5a09`（AI 选靶决策记录）后面。**保持单一 head**：并排挂在同一个
#: 父节点上会变成两个 head，`alembic upgrade head` 直接报「Multiple head revisions」，
#: 而生产的升级机制就是启动时跑它（`web.runtime._upgrade_database`）。
down_revision: str | None = "61eb261c5a09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TARGETS = "bot_targets"
_SEEN_AT = "unreadable_seen_at_utc"
_ATTEMPTS = "unreadable_attempts"
_CONFIG = "military_attack_config"
_EXCLUSION = "unreadable_exclusion_hours"


def upgrade() -> None:
    op.add_column(_TARGETS, sa.Column(_SEEN_AT, sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        _TARGETS,
        sa.Column(_ATTEMPTS, sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(_CONFIG, sa.Column(_EXCLUSION, sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column(_CONFIG, _EXCLUSION)
    op.drop_column(_TARGETS, _ATTEMPTS)
    op.drop_column(_TARGETS, _SEEN_AT)
