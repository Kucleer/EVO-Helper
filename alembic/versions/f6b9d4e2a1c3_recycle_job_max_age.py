"""回收作业时间上限配置列

Revision ID: f6b9d4e2a1c3
Revises: e5a8c3d2f1b4
Create Date: 2026-09-10

回收作业的过期时间上限（运维旋钮，默认 24 小时）。
空 = 跟着代码默认值走。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6b9d4e2a1c3"
down_revision: str | Sequence[str] | None = "e5a8c3d2f1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("military_attack_config") as batch:
        batch.add_column(sa.Column("recycle_job_max_age_hours", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("military_attack_config") as batch:
        batch.drop_column("recycle_job_max_age_hours")
