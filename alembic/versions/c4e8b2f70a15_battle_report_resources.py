"""战报「获得资源」12 格的明细表

Revision ID: c4e8b2f70a15
Revises: c2a8f4d31e75
Create Date: 2026-08-17

新增 `battle_report_resources`：战报详情页未滚动那一屏上「获得资源」那 12 个格子里
**非零**的几格。用户口径（2026-08-17）：只统计这 12 个值。

**为什么存 `slot` 不存资源名。** 位置是观测到的事实，名字是解释。解释错了以后
还能靠 slot 重新映射；把名字硬编进库里，原始观测就找不回来了。翻译放在页面渲染时
（`domain.battle_resources.SLOT_LABELS`），眼下那张对照表还空着——空着不妨碍
数量先记着，这正是这套设计的价值。

**为什么不在 `battle_reports` 上加 12 列。** 那是把关系表当电子表格用：列名一旦
定错就只能靠迁移改，而格数是游戏的排版、不是这套系统的常量。

⚠️ **没有行 = 那一格是 0，不是「没读到」。** 读的时候是全有或全无：12 格但凡有一格
读不出来，这份战报一行都不写（判据在 `domain.battle_resources.parse_resource_grid`）。

`amount` 与 `uncertainty` 用 `BigInteger`：画面上已经出现过 `3.7M`，解析器还认 `B`
后缀，32 位在这条量级上只是等着某天溢出。**这条没有方言分岔**——SQLAlchemy 在
SQLite 上把 `BigInteger` 渲染成 `INTEGER`（本来就是 64 位），在 Postgres 上渲染成
`BIGINT`，两边都对。本表也没有时刻列，所以 `b6e0a4f21c98` 那条 `TIMESTAMP WITH
TIME ZONE` 的规矩在这里用不上。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e8b2f70a15"
down_revision: str | None = "c2a8f4d31e75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "battle_report_resources",
        sa.Column("id", sa.Uuid(), nullable=False),
        # 外键指向 `battle_reports`：这几行的全部意义就是挂在某一份战报上。
        sa.Column("report_id", sa.Uuid(), nullable=False),
        # 网格位置 0..11，行优先。**不是资源名**，理由见模块头。
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("amount", sa.BigInteger(), nullable=False),
        sa.Column("approximate", sa.Boolean(), nullable=False),
        sa.Column("uncertainty", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["battle_reports.id"]),
        sa.PrimaryKeyConstraint("id"),
        # 一份战报的一个格子只能有一行：重复读到同一份战报走的是「库里已有」
        # 那条早停路径，真撞上了宁可写失败也不要攒出两份收获。
        sa.UniqueConstraint("report_id", "slot", name="uq_battle_report_resources_slot"),
    )
    op.create_index(
        op.f("ix_battle_report_resources_report_id"),
        "battle_report_resources",
        ["report_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_battle_report_resources_report_id"), table_name="battle_report_resources"
    )
    op.drop_table("battle_report_resources")
