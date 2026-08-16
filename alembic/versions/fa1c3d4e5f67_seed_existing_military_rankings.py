"""seed the ranking page from military evidence already stored on bot targets

Revision ID: fa1c3d4e5f67
Revises: f8c7a1e4d902
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision: str = "fa1c3d4e5f67"
down_revision: str | Sequence[str] | None = "f8c7a1e4d902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create one immutable snapshot only when a prior import has not done so.

    Older live installations stored military scores directly on ``bot_targets``.
    Re-reading the game just to populate the new fast-filter page would be both
    slow and unnecessary, so retain that already-observed evidence as its first
    snapshot.  ``military_rank`` is nullable in the legacy store and is kept
    nullable here rather than inventing ranks from score order.
    """
    connection = op.get_bind()
    existing = connection.execute(
        sa.text("SELECT COUNT(*) FROM military_ranking_snapshots")
    ).scalar_one()
    if existing:
        return

    rows = connection.execute(
        sa.text(
            "SELECT military_rank, latest_owner_name, military_score, galaxy, system, position "
            "FROM bot_targets "
            "WHERE military_score IS NOT NULL "
            "ORDER BY CASE WHEN military_rank IS NULL THEN 1 ELSE 0 END, "
            "military_rank ASC, military_score DESC, galaxy, system, position"
        )
    ).mappings().all()
    if not rows:
        return

    snapshot_id = uuid4().hex
    connection.execute(
        sa.text(
            "INSERT INTO military_ranking_snapshots (id, captured_at_utc, row_count) "
            "VALUES (:id, :captured_at_utc, :row_count)"
        ),
        {
            "id": snapshot_id,
            "captured_at_utc": datetime.now(UTC),
            "row_count": len(rows),
        },
    )
    connection.execute(
        sa.text(
            "INSERT INTO military_ranking_entries "
            "(id, snapshot_id, ordinal, rank, player_name, score, galaxy, system, position) "
            "VALUES (:id, :snapshot_id, :ordinal, :rank, :player_name, :score, "
            ":galaxy, :system, :position)"
        ),
        [
            {
                "id": uuid4().hex,
                "snapshot_id": snapshot_id,
                "ordinal": ordinal,
                "rank": row["military_rank"],
                # A legacy score can predate the coordinate scan and therefore
                # have no owner name.  The coordinate remains a stable label.
                "player_name": row["latest_owner_name"]
                or f"bot_{row['galaxy']}_{row['system']}_{row['position']}",
                "score": row["military_score"],
                "galaxy": row["galaxy"],
                "system": row["system"],
                "position": row["position"],
            }
            for ordinal, row in enumerate(rows)
        ],
    )


def downgrade() -> None:
    # Snapshots are audit records.  Do not remove a later real scan when a
    # migration is rolled back; the schema rollback handles its own tables.
    pass
