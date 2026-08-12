"""bot 分档的三道边界落库

Revision ID: a3d7b1e64c92
Revises: b6e0a4f21c98
Create Date: 2026-08-12

`scheduler_config` 补三列：`tier_alpha_from` / `tier_beta_from` /
`tier_gamma_from`，各是那一档的下界（闭区间）。

原先这三个数写死在 `domain.fleet_tier.TIER_BOUNDARIES`，改一次要改代码。
用户口径（2026-08-11）是这三个数要可配，**档位数量与预设名 AAA/BBB/CCC 不动**。

默认值 2000 / 4000 / 8000 就是用户给的那一套。⚠️ 中间那道从原先的 5000 改成了
4000——这是用户在同一句话里顺带改的口径，不是笔误。

**不回头重算任何历史行**：分档结论从来没有存过（库里存的是
`battle_reports.defender_units` 这个读数和 `attack_dispatches.preset_name` 这个
实际用掉的预设标题），每次派遣现算。所以这条迁移只加列，一行数据都不动，
新阈值只影响加完之后发出的攻击。

`server_default` 必须给：老库里已经有一行配置，加一列 NOT NULL 而不给默认值，
SQLite 会直接拒绝这条 ALTER。同 `c7e4a1b95d62`，建完不再去掉——SQLite 不支持
改列，而 ORM 那边的 `default=` 与这里取值一致。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3d7b1e64c92"
#: 排在 `b6e0a4f21c98`（时刻列带时区）之后。这条迁移原先也写着 `f4c2e91a7b63`，
#: 那两条就成了 alembic 的两个 head，而 `web.runtime._upgrade_database` 升到
#: `"head"` 会直接抛 "Multiple head revisions are present"——控制台连启动都启动不了。
down_revision: str | None = "b6e0a4f21c98"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 列名 → 默认值。与 `storage.models.SchedulerConfigRow` 上那三列一一对应。
_COLUMNS = (
    ("tier_alpha_from", "2000"),
    ("tier_beta_from", "4000"),
    ("tier_gamma_from", "8000"),
)


def upgrade() -> None:
    for name, default in _COLUMNS:
        op.add_column(
            "scheduler_config",
            sa.Column(name, sa.Integer(), nullable=False, server_default=default),
        )


def downgrade() -> None:
    for name, _default in reversed(_COLUMNS):
        op.drop_column("scheduler_config", name)
