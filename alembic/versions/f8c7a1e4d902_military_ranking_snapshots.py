"""persist military ranking snapshots

Revision ID: f8c7a1e4d902
Revises: f6a4d9c2e801
Create Date: 2026-08-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f8c7a1e4d902"
down_revision: str | None = "f6a4d9c2e801"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "military_ranking_snapshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("captured_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_military_ranking_snapshots_captured_at_utc",
        "military_ranking_snapshots",
        ["captured_at_utc"],
    )
    op.create_table(
        "military_ranking_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("player_name", sa.String(length=128), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("galaxy", sa.Integer(), nullable=True),
        sa.Column("system", sa.Integer(), nullable=True),
        sa.Column("position", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["snapshot_id"], ["military_ranking_snapshots.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", "ordinal", name="uq_military_ranking_snapshot_ordinal"),
    )
    op.create_index(
        "ix_military_ranking_entries_snapshot_id", "military_ranking_entries", ["snapshot_id"]
    )
    op.create_index("ix_military_ranking_entries_rank", "military_ranking_entries", ["rank"])
    op.create_index(
        "ix_military_ranking_entries_coordinate",
        "military_ranking_entries",
        ["galaxy", "system", "position"],
    )


def downgrade() -> None:
    op.drop_table("military_ranking_entries")
    op.drop_table("military_ranking_snapshots")
