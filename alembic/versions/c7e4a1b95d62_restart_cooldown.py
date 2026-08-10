"""调度器的重启冷却

Revision ID: c7e4a1b95d62
Revises: b4d81f60c9a2
Create Date: 2026-08-09

`scheduler_config` 补一列 `restart_cooldown_seconds`（默认 300 秒）。

它堵的是「立即收取」的空转：`expected_report_at_utc` 为 NULL 时战报判据恒为
「该去收」，而战报可能只是还没到。runner 进信箱、扑空、退出、下一 tick 判据
仍为真、再起一次——不是死循环，但每轮几十秒的导航全白费，还一直占着鼠标不让
扫描进来。

`server_default` 必须给：老库里已经有一行配置，加一列 NOT NULL 而不给默认值，
SQLite 会直接拒绝这条 ALTER。建完再去掉 server_default 没有意义（SQLite 不支持
改列），所以就让它留在表定义里——ORM 那边的 `default=300` 管新插入的行，
两者取值一致。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7e4a1b95d62"
down_revision: str | None = "b4d81f60c9a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "scheduler_config",
        sa.Column("restart_cooldown_seconds", sa.Integer(), nullable=False, server_default="300"),
    )


def downgrade() -> None:
    op.drop_column("scheduler_config", "restart_cooldown_seconds")
