"""给 battle_reports.reported_at_utc 加索引

Revision ID: d3b9f27c4a81
Revises: c7d92f4a1b60
Create Date: 2026-09-07

开封每一封邮件之前都要先问一句「这个报告时刻的战报在不在库里」
（`storage.repository.has_report_at`），而这张表上原先只有 `battle_reports_pkey`(id)
和 `battle_reports_dispatch_id_key`(dispatch_id) 两个索引 —— 那一问一次都用不上，
每封邮件换来一次整表扫。表现在 2969 行，只增不减。

⚠️ **这条迁移不改任何行为**，加的是纯读取路径上的索引：没有新列、没有新约束、
没有回填。所以它可以独立于耗时优化那一摊先发，发早了也不会有半成品挂在库上。

⚠️ **不走 `batch_alter_table`**：`CREATE INDEX` / `DROP INDEX` 在 SQLite 与
PostgreSQL 上都原生支持，不需要重建整张表 —— 而重建这张表会把它上面的外键与那条
`dispatch_id` 唯一约束重新生成一遍，代价远大于要办的事。

⚠️ **回退只删索引，一行数据都不动。** 索引不承载语义，退回去只是查询变慢。
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d3b9f27c4a81"
#: 接在 `c7d92f4a1b60`（撤掉 `blind_scrolls` 那一列）后面，也就是当时的 head。
#: **保持单一 head**：并排挂在同一个父节点上会变成两个 head，`alembic upgrade head`
#: 直接报「Multiple head revisions」，而生产的升级机制就是启动时跑它
#: （`web.runtime._upgrade_database`）——症状是用户重启 bat 之后控制台起不来。
down_revision: str | Sequence[str] | None = "c7d92f4a1b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "battle_reports"
_COLUMN = "reported_at_utc"
_INDEX = "ix_battle_reports_reported_at_utc"


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
