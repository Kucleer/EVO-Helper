"""军力攻击可配置多个出发星球。"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f5b2c3d4e5f6"
# 合并既有的日攻击状态与军力榜两条迁移支线；否则 runtime 升级 ``head`` 会因
# 多头直接拒绝启动，用户连页面都进不去。
down_revision: str | Sequence[str] | None = ("f3a91c2d4e07", "f4c2e91a7b63")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mission_task_origins",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "task_id",
            sa.Integer(),
            sa.ForeignKey("mission_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("galaxy", sa.Integer(), nullable=False),
        sa.Column("system", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("fleet_lines", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("task_id", "galaxy", "system", "position", name="uq_task_origin"),
    )
    op.create_index("ix_mission_task_origins_task_id", "mission_task_origins", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_mission_task_origins_task_id", table_name="mission_task_origins")
    op.drop_table("mission_task_origins")
