"""攻击意图记下打的是 bot 还是海盗

Revision ID: f2b9d3c07a41
Revises: e1a7c4b2f905
Create Date: 2026-08-09

攻击日志的第一个问题是「这一发是打谁的」。bot 与海盗走的是两条判定链路、
两套预设、两种收益，事后混在一起就没法分别评估。

存量行一律算 `bot`：这个字段加进来之前，只有 bot 攻击链路会写意图。
不设可空——「不知道打的是谁」在日志里没有意义，缺省值必须是一个事实。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f2b9d3c07a41"
down_revision: str | None = "e1a7c4b2f905"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("attack_intents") as batch:
        batch.add_column(
            sa.Column(
                "target_kind",
                sa.String(length=16),
                nullable=False,
                server_default="bot",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("attack_intents") as batch:
        batch.drop_column("target_kind")
