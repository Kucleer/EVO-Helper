"""删掉 `scheduler_config` 上的三道分档边界

Revision ID: c1f70b8a26d4
Revises: a3d7b1e64c92
Create Date: 2026-08-13

用户口径（2026-08-13）：bot 不再做攻击侦查、不再分档，一律用预设 BBB 打。
分档整套（`domain.fleet_tier`、`/tiers` 页、`--tier-thresholds` 参数）随之删除，
`scheduler_config` 上那三列（`tier_alpha_from` / `tier_beta_from` /
`tier_gamma_from`，由 `a3d7b1e64c92` 于前一天加上）也就没有读者了。

**留着不删的代价不是磁盘。** 那三列有值、有默认值，看起来像还生效的配置；
下一个翻库的人（或下一个 agent）会照着它去找「阈值是在哪儿用的」，而答案是
「没有任何地方用」。列在、代码不在，是最难查的那种不一致。

一行业务数据都不动：分档结论从来没有存过——库里存的是
`battle_reports.defender_units` 这个读数和 `attack_dispatches.preset_name` 这个
实际用掉的预设标题，两者都在别的表上，这条迁移碰不到。历史上那些
`preset_name` 为 AAA / CCC 的派遣照旧留着，它们记的是当时真的用了哪套预设，
不是「按今天的规则该用哪套」。

SQLite 不支持 `ALTER TABLE ... DROP COLUMN`（3.35 之前），所以走
`batch_alter_table`：它建新表、搬数据、换名。`downgrade()` 把三列原样加回来，
带回 `a3d7b1e64c92` 那一版的 `server_default`——不带的话老库已有行会让
`ADD COLUMN NOT NULL` 直接被 SQLite 拒掉，往返就走不通。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1f70b8a26d4"
down_revision: str | None = "a3d7b1e64c92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 列名 → 回滚时要还回去的默认值。与 `a3d7b1e64c92._COLUMNS` 逐字一致。
_COLUMNS = (
    ("tier_alpha_from", "2000"),
    ("tier_beta_from", "4000"),
    ("tier_gamma_from", "8000"),
)


def upgrade() -> None:
    with op.batch_alter_table("scheduler_config") as batch:
        for name, _default in _COLUMNS:
            batch.drop_column(name)


def downgrade() -> None:
    with op.batch_alter_table("scheduler_config") as batch:
        for name, default in _COLUMNS:
            batch.add_column(
                sa.Column(name, sa.Integer(), nullable=False, server_default=default)
            )
