"""planet_scout_alerts: persist foreign reconnaissance alerts and delivery state

Revision ID: f6a4d9c2e801
Revises: d2c4b8a71f39
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f6a4d9c2e801"
down_revision: str | None = "d2c4b8a71f39"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "planet_scout_alerts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("reported_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_time_text", sa.String(length=64), nullable=False),
        sa.Column("source_galaxy", sa.Integer(), nullable=False),
        sa.Column("source_system", sa.Integer(), nullable=False),
        sa.Column("source_position", sa.Integer(), nullable=False),
        sa.Column("target_galaxy", sa.Integer(), nullable=False),
        sa.Column("target_system", sa.Integer(), nullable=False),
        sa.Column("target_position", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=True),
        sa.Column("intercepted_probes", sa.Integer(), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("raw_body", sa.Text(), nullable=False),
        sa.Column(
            "delivery_status", sa.String(length=32), nullable=False, server_default="PENDING"
        ),
        sa.Column("delivery_error", sa.Text(), nullable=True),
        sa.Column("delivered_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_planet_scout_alerts_fingerprint"),
    )
    op.create_index(
        "ix_planet_scout_alerts_reported_at_utc",
        "planet_scout_alerts",
        ["reported_at_utc"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_planet_scout_alerts_reported_at_utc", table_name="planet_scout_alerts")
    op.drop_table("planet_scout_alerts")
