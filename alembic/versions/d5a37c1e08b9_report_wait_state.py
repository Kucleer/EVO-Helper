"""记录飞行时间与松手等待的唤醒时间

Revision ID: d5a37c1e08b9
Revises: c3f81a97b2d4
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5a37c1e08b9"
down_revision: str | None = "c3f81a97b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attack_dispatches") as batch:
        # 既有派遣没有飞行时间，取 NULL：等待调度器会改为立即尝试收取。
        batch.add_column(sa.Column("flight_seconds", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("expected_report_at_utc", sa.DateTime(), nullable=True))

    with op.batch_alter_table("run_instances") as batch:
        batch.add_column(sa.Column("resume_at_utc", sa.DateTime(), nullable=True))
        batch.add_column(
            sa.Column("session_attempts", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("run_instances") as batch:
        batch.drop_column("session_attempts")
        batch.drop_column("resume_at_utc")

    with op.batch_alter_table("attack_dispatches") as batch:
        batch.drop_column("expected_report_at_utc")
        batch.drop_column("flight_seconds")
