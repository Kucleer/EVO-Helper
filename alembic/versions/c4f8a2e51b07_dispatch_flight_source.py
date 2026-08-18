"""记下每一发的飞行时长是**谁给的**

Revision ID: c4f8a2e51b07
Revises: b7e4d0c93a15
Create Date: 2026-08-19

`attack_dispatches` 上的两个钟（`expected_report_at_utc` / `line_free_at_utc`）
从此可能有三种来历，可信度差着数量级：

- `briefing_arrival`  简报页「预计到达时间」减去读屏时刻。**主来源**，
  49 张失败现场实测 47/47、零读错。
- `briefing_duration` 简报页「飞行时间」那一行的 OCR。同一批实拍上 0/47——
  `分` 被读成 `5)`、`秒` 被读成 `%`，是确定性失败，重试与换配方都救不回来。
- `distance_model`    `domain.flight_time` 的距离公式。**算出来的，不是读出来的。**

不加这一列的话，事后查账分不清「这个 90 分钟是读出来的还是算出来的」，而本仓
硬规矩是「猜出来的数不许长得像量出来的」（`storage.models` 里
`target_military_score_estimated` 那一段是同形先例）。

⚠️ **可空，而且刻意不给 `server_default`。** 存量 838 行根本不知道当时那个数
怎么来的，默认成 `briefing_duration` 是让历史行冒充实读；NULL 才是实话。

⚠️ 与它配套的一条**不在这个迁移里、但必须一起读**：来源是 `distance_model` 时
`flight_seconds` 仍然写 NULL（见 `storage.repository.record_flight_time`）。
那一列是 `domain.report_wait.vet_flight_time` 那道下限赖以标定的样本池，掺进
公式自己的输出，下一次标定就成了拿模型的输出去标定模型。

可空列的 `ADD COLUMN` 两种方言（SQLite / PostgreSQL）都直接支持，不必走
`batch_alter_table` 那条重建整张表的路。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4f8a2e51b07"
#: 接在 `b7e4d0c93a15`（撞上保护期）后面。**保持单一 head**：并排挂在同一个父
#: 节点上会变成两个 head，`alembic upgrade head` 直接报「Multiple head revisions」，
#: 而生产的升级机制就是启动时跑它（`web.runtime._upgrade_database`）。
#: 合并时若发现别的分支也挂在 `b7e4d0c93a15` 上，**后进的那条改自己的
#: `down_revision`**，别去动已经合进去的。
down_revision: str | None = "b7e4d0c93a15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "attack_dispatches"
_COLUMN = "flight_source"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(length=24), nullable=True))


def downgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)
