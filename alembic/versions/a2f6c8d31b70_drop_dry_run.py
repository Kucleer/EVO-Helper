"""删掉演习模式（dry_run）两列

Revision ID: a2f6c8d31b70
Revises: d18b3f5c07ae
Create Date: 2026-08-11

演习模式这个概念被整体移除：派遣就是真派遣，不再有第二条路径。代码里没有
任何地方读写这两列了，留着只会让人以为还存在一个开关。

**`attack_dispatches.dry_run`** 原先是「这一发只是记账、没有舰队飞出去」的
标记，配额统计、在飞航线数、等战报、战报匹配四处查询都靠它把演习记录排除在外。
删掉之后那四处只剩 `accepted` 这一半过滤——**那一半必须留着**：被游戏拒掉的
派遣同样收不到战报，算进来就成了一条「已派出且永远收不到战报」的死记录。

**`scan_plans.dry_run`** 是计划表上的同名配置项，从来没有被派遣链路读过
（只出现在 Web 的计划详情里），删掉不改变任何运行时行为。

**存量数据不受影响。** 2026-08-11 核对生产库：`attack_dispatches` 共 14 行，
`dry_run` **全部为 0**，被删掉的那半个过滤条件历史上一行都没排除过——不会有
历史记录因为删列而被重新算成真实派遣，上面那四处查询的结果与迁移前逐条相同。
`scan_plans` 里有 2 行为 1，但如上所述没有读者。

降级把两列加回来，默认值取原先的建表默认：派遣按「真实」（0）回填，
计划按原 ORM 默认（1）回填。回填值只是形状还原，不代表原始数据。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a2f6c8d31b70"
down_revision: str | None = "d18b3f5c07ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite 不支持 DROP COLUMN 的完整语义，必须走 batch（重建表 + 搬数据）。
    with op.batch_alter_table("attack_dispatches") as batch:
        batch.drop_column("dry_run")
    with op.batch_alter_table("scan_plans") as batch:
        batch.drop_column("dry_run")


def downgrade() -> None:
    with op.batch_alter_table("scan_plans") as batch:
        batch.add_column(
            sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.true())
        )
    with op.batch_alter_table("attack_dispatches") as batch:
        batch.add_column(
            sa.Column("dry_run", sa.Boolean(), nullable=False, server_default=sa.false())
        )
