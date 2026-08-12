"""调度器的三张表

Revision ID: e29d06f8489f
Revises: a91c6d4e8b07
Create Date: 2026-08-09

常驻调度器要在三条任务链路之间挑一条起子进程，这需要三样持久化的东西：

- `mission_tasks`：每种任务一行，优先级由用户拖出来。参数存 JSON 而不是逐列，
  以后加任务种类不用再动表结构。
- `mission_runs`：每起一个子进程一行，记下 pid——控制台重启后靠它认出可能
  还活着的孤儿进程。
- `scheduler_config`：单行。航线是全局资源，不属于任何单个任务。

只建这三张表。autogenerate 另外报了一条 `scan_plans` 上的唯一约束差异
（`uq_scan_plans_public_id`，SQLite 里索引与约束的表示对不上导致的假差异），
已从本迁移中删除——本次设计明确不碰既有的表。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e29d06f8489f"
down_revision: str | None = "a91c6d4e8b07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mission_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("started_at_utc", sa.DateTime(), nullable=False),
        sa.Column("ended_at_utc", sa.DateTime(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("stopped_by", sa.String(length=16), nullable=True),
        sa.Column("log_path", sa.String(length=255), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_mission_runs_kind"), "mission_runs", ["kind"], unique=False)
    op.create_table(
        "mission_tasks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("round_started_at_utc", sa.DateTime(), nullable=True),
        sa.Column("quota_exhausted_until_utc", sa.DateTime(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("disabled_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind"),
    )
    op.create_table(
        "scheduler_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fleet_line_limit", sa.Integer(), nullable=False),
        sa.Column("reserved_lines", sa.Integer(), nullable=False),
        sa.Column("pirate_daily_quota", sa.Integer(), nullable=False),
        sa.Column("min_dwell_seconds", sa.Integer(), nullable=False),
        sa.Column("report_grace_minutes", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("scheduler_config")
    op.drop_table("mission_tasks")
    op.drop_index(op.f("ix_mission_runs_kind"), table_name="mission_runs")
    op.drop_table("mission_runs")
