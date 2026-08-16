"""move military tiers and origin planets into global configuration

Revision ID: f6c3d2a1b4e8
Revises: f5b2c3d4e5f6
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f6c3d2a1b4e8"
down_revision: str | Sequence[str] | None = "f5b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attack_planets",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("sort_index", sa.Integer(), nullable=False, unique=True),
        sa.Column("galaxy", sa.Integer(), nullable=False),
        sa.Column("system", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("galaxy", "system", "position", name="uq_attack_planet_coordinate"),
    )
    op.create_table(
        "military_attack_config",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("tiers_json", sa.Text(), nullable=False, server_default="[]"),
    )
    with op.batch_alter_table("mission_task_origins") as batch:
        batch.add_column(sa.Column("planet_id", sa.Integer(), nullable=True))
        batch.create_index("ix_mission_task_origins_planet_id", ["planet_id"])
        batch.create_foreign_key(
            "fk_mission_task_origins_planet_id",
            "attack_planets",
            ["planet_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    connection = op.get_bind()
    origins = connection.execute(
        sa.text(
            "SELECT DISTINCT galaxy, system, position FROM mission_task_origins "
            "ORDER BY galaxy, system, position"
        )
    ).mappings()
    for index, origin in enumerate(origins, start=1):
        result = connection.execute(
            sa.text(
                "INSERT INTO attack_planets (sort_index, galaxy, system, position) "
                "VALUES (:sort_index, :galaxy, :system, :position)"
            ),
            {"sort_index": index, **origin},
        )
        planet_id = result.lastrowid
        connection.execute(
            sa.text(
                "UPDATE mission_task_origins SET planet_id = :planet_id "
                "WHERE galaxy = :galaxy AND system = :system AND position = :position"
            ),
            {"planet_id": planet_id, **origin},
        )

    tiers_json = "[]"
    tasks = connection.execute(
        sa.text("SELECT params_json FROM mission_tasks ORDER BY id")
    ).scalars()
    for raw in tasks:
        try:
            tiers = json.loads(raw).get("tiers")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(tiers, list):
            tiers_json = json.dumps(tiers, ensure_ascii=False)
            break
    connection.execute(
        sa.text("INSERT INTO military_attack_config (id, tiers_json) VALUES (1, :tiers_json)"),
        {"tiers_json": tiers_json},
    )


def downgrade() -> None:
    with op.batch_alter_table("mission_task_origins") as batch:
        batch.drop_constraint("fk_mission_task_origins_planet_id", type_="foreignkey")
        batch.drop_index("ix_mission_task_origins_planet_id")
        batch.drop_column("planet_id")
    op.drop_table("military_attack_config")
    op.drop_table("attack_planets")
