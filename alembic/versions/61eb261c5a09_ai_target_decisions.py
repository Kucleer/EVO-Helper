"""AI 选靶影子观测：记录每一轮算法与 LLM 各自选了什么。

Revision ID: 61eb261c5a09
Revises: d1a7f4b26c93
Create Date: 2026-08-19

一期（影子模式）的落库：调度器照常用算法派遣，另把 AI 的候选选择
原样存下来供对比与自动核对。**表只存，不影响任何派遣判据。**

- `prompt_text` / `response_text` 刻意**原样存**：模型换版本答案就变，
  事后复盘时「当时喂进去的到底是什么」是唯一能对账的东西。
- `decided_at_utc` 是**产生时刻**，不是入库时刻——fire-and-forget 线程里
  组装 prompt 与落库之间隔着一次网络往返，拿入库时刻当产生时刻会把
  时间线扭曲（见 `system_log.logged_at_utc` 的同款口径）。
- `status` 枚举在 `domain.ai_targeting.AiDecisionStatus`：
  `ok` / `timeout` / `http_error` / `invalid_json` / `schema_violation`。
  任何失败都静默降级（记一行 + 记日志），**绝不影响派遣**。
- `id` 用 `BigInteger().with_variant(Integer, "sqlite")`，同 `system_log`：
  SQLite 上必须写成 `INTEGER PRIMARY KEY` 才会自增（见 `SystemLogRow` 那条注释）。
- `run_id` 可空、**不带 CASCADE**，同 `system_log`：那一行是账，任务删了也要留。

同时给 `military_attack_config` 加五个 AI 旋钮。**全部可空、一律不给
`server_default`**：NULL = 「跟着代码里的默认值走」，给了默认值就分不开
「没配」和「恰好配成当前默认」，日后调默认值时老行会被钉死在旧数上
（先例是 `blind_scrolls` 那一段注释）。

量级：约 26 轮/天 × 7 KB ≈ 180 KB/天 ≈ 66 MB/年。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "61eb261c5a09"
#: 接在 `d1a7f4b26c93`（航线占用的按距离下界）后面。当前唯一 head，
#: 生产库 `alembic_version` 实测已停在这一条上（2026-08-19 只读查询）。
down_revision: str | None = "d1a7f4b26c93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ai_target_decisions"
_CONFIG_TABLE = "military_attack_config"

#: 新表的主键。`with_variant(Integer, "sqlite")` 不是可选的——纯 `BigInteger`
#: 在 SQLite 上建出来是 `BIGINT`，而 SQLite 只把写成 `INTEGER PRIMARY KEY` 的
#: 列当 rowid 别名，插入不带 id 当场 `NOT NULL constraint failed`（同
#: `storage.models.SystemLogRow` 那条注释）。
_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", _ID, primary_key=True, autoincrement=True),
        sa.Column("decided_at_utc", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("task_id", sa.Integer(), nullable=True, index=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("cycle_start_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget", sa.Integer(), nullable=False),
        sa.Column("algorithm_picks_json", sa.Text(), nullable=False),
        sa.Column("ai_picks_json", sa.Text(), nullable=True),
        sa.Column("overlap", sa.Integer(), nullable=True),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("violations_json", sa.Text(), nullable=True),
    )
    for column in (
        "ai_shadow_enabled",
        "ai_model",
        "ai_timeout_seconds",
        "ai_sample_size",
        "ai_retention_days",
    ):
        if column == "ai_shadow_enabled":
            column_type: sa.types.TypeEngine[object] = sa.Boolean()
        elif column == "ai_model":
            column_type = sa.String(length=64)
        else:
            column_type = sa.Integer()
        op.add_column(_CONFIG_TABLE, sa.Column(column, column_type, nullable=True))


def downgrade() -> None:
    for column in (
        "ai_retention_days",
        "ai_sample_size",
        "ai_timeout_seconds",
        "ai_model",
        "ai_shadow_enabled",
    ):
        op.drop_column(_CONFIG_TABLE, column)
    op.drop_table(_TABLE)
