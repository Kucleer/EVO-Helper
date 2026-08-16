"""store diagnostic output in a system_log table

Revision ID: a7f2c9d40b16
Revises: fa1c3d4e5f67
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7f2c9d40b16"
down_revision: str | None = "fa1c3d4e5f67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: ⚠️ **两种方言的自增主键行为不一样，这一行是实测出来的（2026-08-16）。**
#:
#: 纯 `sa.BigInteger` 在 SQLite 上建出 `BIGINT`，而 SQLite 只把写成
#: `INTEGER PRIMARY KEY` 的列当 rowid 别名——插入不带 id 会当场
#: `IntegrityError: NOT NULL constraint failed: system_log.id`，自增压根不发生。
#: 加上 `with_variant` 之后：SQLite 是 `INTEGER`（自增可用），
#: PostgreSQL 仍然是 `BIGSERIAL`。ORM 那边（`storage/models.py`）写的是同一句。
_ID_TYPE = sa.BigInteger().with_variant(sa.Integer, "sqlite")


def upgrade() -> None:
    op.create_table(
        "system_log",
        sa.Column("id", _ID_TYPE, autoincrement=True, nullable=False),
        # `timezone=True` 不能省：Postgres 上没有它就是
        # `TIMESTAMP WITHOUT TIME ZONE`，tzinfo 被静默截掉（见 `UTCDateTime` 的注释）。
        sa.Column("logged_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("host", sa.String(length=64), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("mission_kind", sa.String(length=16), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), server_default="{}", nullable=False),
        # 刻意**不带 ondelete CASCADE**：日志是事后翻账用的，删掉一行 `mission_runs`
        # 不该顺手把「那一轮到底发生了什么」也一起删掉。
        sa.ForeignKeyConstraint(["run_id"], ["mission_runs.id"], name="fk_system_log_run_id"),
        sa.PrimaryKeyConstraint("id", name="pk_system_log"),
    )
    op.create_index("ix_system_log_logged_at_id", "system_log", ["logged_at_utc", "id"])
    op.create_index("ix_system_log_run_id_id", "system_log", ["run_id", "id"])
    op.create_index("ix_system_log_host_logged_at", "system_log", ["host", "logged_at_utc"])
    op.create_index("ix_system_log_level_logged_at", "system_log", ["level", "logged_at_utc"])


def downgrade() -> None:
    # 索引跟着表一起走；显式 drop 是为了在 Postgres 上也不留孤儿对象。
    op.drop_index("ix_system_log_level_logged_at", table_name="system_log")
    op.drop_index("ix_system_log_host_logged_at", table_name="system_log")
    op.drop_index("ix_system_log_run_id_id", table_name="system_log")
    op.drop_index("ix_system_log_logged_at_id", table_name="system_log")
    op.drop_table("system_log")
