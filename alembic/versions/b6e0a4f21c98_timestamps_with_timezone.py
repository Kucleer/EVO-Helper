"""store every business timestamp as TIMESTAMP WITH TIME ZONE

Revision ID: b6e0a4f21c98
Revises: f4c2e91a7b63
Create Date: 2026-08-12

把 34 个业务时刻列从 ``TIMESTAMP WITHOUT TIME ZONE`` 改成 ``WITH TIME ZONE``，
配合 ``storage.database.UTCDateTime`` 的 ``impl = DateTime(timezone=True)``。

**为什么现在做**：Postgres 的 ``TIMESTAMP WITHOUT TIME ZONE`` 会把 tzinfo 静默截掉，
读回来变成 naive。本项目的判据（配额按 UTC 日切、``round_started_at`` 分轮、
战报时间比较）全部建立在「读出来是 aware 的 UTC」上，一个 naive 值进来不会报错，
只会让这些判断安静地错。所以在换库**之前**先把列语义定死，两件事分开验证。

**SQLite 上这是真正的无操作**，见 ``upgrade()`` 里的说明。

⚠️ ``USING ... AT TIME ZONE 'UTC'`` 不能省。存量的 naive 值全是 UTC 时刻
（``UTCDateTime.process_bind_param`` 一直先 ``astimezone(UTC)``），而裸转换会让
Postgres 按**会话时区**去解释它们——服务器不在 UTC 时整库偏几个小时。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b6e0a4f21c98"
down_revision: str | None = "f4c2e91a7b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


#: ``(表, 列, 可空)``。与 ``storage.models`` 里所有 ``UTCDateTime`` 列一一对应。
#: 逐条列出而不是反射 metadata：迁移描述的是**当时**的库结构，
#: 跟着活的模型走会让这一步在将来模型再变时悄悄改变含义。
_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("artifacts", "created_at_utc", False),
    ("attack_dispatches", "dispatched_at_utc", False),
    ("attack_dispatches", "expected_report_at_utc", True),
    ("attack_dispatches", "line_free_at_utc", True),
    ("attack_intents", "created_at_utc", False),
    ("attack_intents", "cycle_start_utc", False),
    ("battle_reports", "reported_at_utc", False),
    ("bot_targets", "last_attack_at_utc", True),
    ("bot_targets", "last_dispatch_at_utc", True),
    ("bot_targets", "last_report_at_utc", True),
    ("bot_targets", "last_scanned_at_utc", True),
    ("coordinate_scans", "scanned_at_utc", False),
    ("daily_reconciliations", "reconciled_at_utc", False),
    ("intel_filters", "created_at_utc", False),
    ("intel_filters", "updated_at_utc", False),
    ("mission_runs", "ended_at_utc", True),
    ("mission_runs", "started_at_utc", False),
    ("mission_tasks", "created_at_utc", False),
    ("mission_tasks", "quota_exhausted_until_utc", True),
    ("mission_tasks", "round_started_at_utc", True),
    ("mission_tasks", "updated_at_utc", False),
    ("run_instances", "created_at_utc", False),
    ("run_instances", "drained_at_utc", True),
    ("run_instances", "finished_at_utc", True),
    ("run_instances", "resume_at_utc", True),
    ("run_instances", "started_at_utc", True),
    # 语义上是「一个自然日」而不是一个时刻，但存量写法是当天的 UTC 00:00:00，
    # 换成 TIMESTAMPTZ 后仍然精确往返。真要收成 DATE 是另一件事（见 PR 说明），
    # 不在这次范围里——这次只统一时区语义，不改列的类型族。
    ("run_instances", "target_date", True),
    ("scan_plans", "created_at_utc", False),
    ("scan_plans", "updated_at_utc", False),
    ("scout_reports", "reported_at_utc", False),
    ("state_events", "occurred_at_utc", False),
    ("target_revisits", "executed_at_utc", True),
    ("target_revisits", "requested_at_utc", False),
    ("ui_observations", "observed_at_utc", False),
)


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if _is_sqlite():
        # SQLite 上什么都不做，而且这不是偷懒：
        # 1. `DateTime()` 与 `DateTime(timezone=True)` 在 SQLite 方言上渲染出的
        #    建表类型**都是 `DATETIME`**，改了也是同一句 DDL；
        # 2. SQLite 本来就把 datetime 存成不带偏移量的字符串，落盘内容一字不变。
        # 唯一「能做」的动作是 `batch_alter_table`——那会把 15 张表连同外键、索引、
        # 唯一约束整个重建一遍，拿生产库的行去换一份一模一样的 DDL。不值当。
        return
    for table, column, nullable in _COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(),
            type_=sa.DateTime(timezone=True),
            existing_nullable=nullable,
            postgresql_using=f'"{column}" AT TIME ZONE \'UTC\'',
        )


def downgrade() -> None:
    if _is_sqlite():
        return
    for table, column, nullable in _COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.DateTime(timezone=True),
            type_=sa.DateTime(),
            existing_nullable=nullable,
            # 反向同样要写死 UTC：裸转换会按会话时区取挂钟时间，
            # 那样降级一次就把整库的时刻平移掉了。
            postgresql_using=f'"{column}" AT TIME ZONE \'UTC\'',
        )
