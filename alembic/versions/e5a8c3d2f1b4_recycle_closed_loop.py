"""残骸回收闭环：配置列 + 决策表 + 作业表

Revision ID: e5a8c3d2f1b4
Revises: d3b9f27c4a81
Create Date: 2026-09-10

三样东西：

1. `military_attack_config.recycle_rate_tenths` —— 回收节奏滑块（整数十分位 0–10）
2. `recycle_decisions` —— 每次航线释放一行，只追加
3. `recycle_jobs` —— 只有选中的才有，状态可变

⚠️ **`recycle_decisions.run_id` 刻意不挂外键** —— `mission_runs` 那一行在
`supervisor.start()` 之后才写，而 run_id 在起子进程之前就生成了。挂了约束
会让决策行在那个窗口里写不进去。

⚠️ **SQLite 上加唯一约束走 batch mode**（本地全量测试跑 SQLite，CI 才是 PostgreSQL）。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a8c3d2f1b4"
down_revision: str | Sequence[str] | None = "d3b9f27c4a81"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("military_attack_config") as batch:
        batch.add_column(sa.Column("recycle_rate_tenths", sa.Integer(), nullable=True))

    op.create_table(
        "recycle_decisions",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("source_dispatch_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("target_galaxy", sa.Integer(), nullable=False),
        sa.Column("target_system", sa.Integer(), nullable=False),
        sa.Column("target_position", sa.Integer(), nullable=False),
        sa.Column("origin_galaxy", sa.Integer(), nullable=False),
        sa.Column("origin_system", sa.Integer(), nullable=False),
        sa.Column("origin_position", sa.Integer(), nullable=False),
        sa.Column("decided_at_utc", sa.DateTime(), nullable=False),
        sa.Column("rate_tenths", sa.Integer(), nullable=False),
        sa.Column("acc_before", sa.Integer(), nullable=False),
        sa.Column("acc_after", sa.Integer(), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("source", sa.String(length=16), server_default="slider", nullable=False),
        sa.ForeignKeyConstraint(["source_dispatch_id"], ["attack_dispatches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_dispatch_id", name="uq_recycle_decision_source"),
        sa.UniqueConstraint(
            "run_id",
            "target_galaxy",
            "target_system",
            "target_position",
            name="uq_recycle_decision_run_target",
        ),
    )
    op.create_index("ix_recycle_decision_decided_at", "recycle_decisions", ["decided_at_utc"])

    op.create_table(
        "recycle_jobs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), autoincrement=True, nullable=False),
        sa.Column("decision_id", sa.Integer(), nullable=False),
        sa.Column("target_galaxy", sa.Integer(), nullable=False),
        sa.Column("target_system", sa.Integer(), nullable=False),
        sa.Column("target_position", sa.Integer(), nullable=False),
        sa.Column("origin_galaxy", sa.Integer(), nullable=False),
        sa.Column("origin_system", sa.Integer(), nullable=False),
        sa.Column("origin_position", sa.Integer(), nullable=False),
        sa.Column("created_at_utc", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("executed_at_utc", sa.DateTime(), nullable=True),
        sa.Column("dispatch_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["decision_id"], ["recycle_decisions.id"]),
        sa.ForeignKeyConstraint(["dispatch_id"], ["attack_dispatches.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("decision_id"),
    )
    op.create_index(
        "ix_recycle_job_pending_target",
        "recycle_jobs",
        ["target_galaxy", "target_system", "target_position"],
    )
    op.create_index("ix_recycle_job_created_at", "recycle_jobs", ["created_at_utc"])


def downgrade() -> None:
    op.drop_index("ix_recycle_job_created_at", table_name="recycle_jobs")
    op.drop_index("ix_recycle_job_pending_target", table_name="recycle_jobs")
    op.drop_table("recycle_jobs")
    op.drop_index("ix_recycle_decision_decided_at", table_name="recycle_decisions")
    op.drop_table("recycle_decisions")
    with op.batch_alter_table("military_attack_config") as batch:
        batch.drop_column("recycle_rate_tenths")
