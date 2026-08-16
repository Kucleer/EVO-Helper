"""record when each military ranking row was read, not just when the batch landed

Revision ID: ab1c7d2e93f4
Revises: fa1c3d4e5f67
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "ab1c7d2e93f4"
down_revision: str | Sequence[str] | None = "fa1c3d4e5f67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """加行级读取时刻，并按所属快照回填存量行。

    用户口径（2026-08-16）：「军力榜我需要的是每条数据的更新时间」。原先只有
    `military_ranking_snapshots.captured_at_utc` 一个快照级时刻，而一趟读榜要滚
    几十屏，行与行之间差得开，快照时刻回答不了「这一行是什么时候读到的」。

    三步走（SQLite 不能直接往有数据的表上加非空列，所以不能一步到位）：
    先加可空列 → 回填 → 再置非空。这条路数照抄 `8c41b9d201ff`。

    ⚠️ **回填 SQL 必须两种方言都能跑。** 本仓库在 `8c41b9d201ff` 上栽过一次：
    那里写死了 SQLite 的 `randomblob(16)`，切 PostgreSQL 时整条升级链断在语句
    解析阶段（`UndefinedFunction`，空库都过不去）。所以这里只用相关子查询——
    它是标准 SQL，SQLite 与 PostgreSQL 行为一致，也没有 `GROUP BY` 可踩
    （PostgreSQL 会对没进 `GROUP BY` 的列报 `GroupingError`，而 SQLite 容忍，
    测试跑 SQLite 根本发现不了）。

    存量规模：生产库 2026-08-16 实测 1 个快照、1,705 行（`fa1c3d4e5f67` 从
    `bot_targets` 播种的那一批）。一条 UPDATE 走完，没有分批的必要。
    """
    with op.batch_alter_table("military_ranking_entries") as batch:
        batch.add_column(sa.Column("observed_at_utc", sa.DateTime(timezone=True), nullable=True))
    # 回填取所属快照的 `captured_at_utc`，而不是 `now()`：这一列要回答的是
    # 「什么时候读到的」。存量行的真实逐屏时刻早已不可考，快照时刻是现存最接近
    # 的真话；写迁移执行的时刻则会把整批数据谎报成刚刚更新。
    op.execute(
        "UPDATE military_ranking_entries SET observed_at_utc = ("
        "SELECT s.captured_at_utc FROM military_ranking_snapshots AS s "
        "WHERE s.id = military_ranking_entries.snapshot_id"
        ") WHERE observed_at_utc IS NULL"
    )
    with op.batch_alter_table("military_ranking_entries") as batch:
        batch.alter_column(
            "observed_at_utc", existing_type=sa.DateTime(timezone=True), nullable=False
        )


def downgrade() -> None:
    with op.batch_alter_table("military_ranking_entries") as batch:
        batch.drop_column("observed_at_utc")
