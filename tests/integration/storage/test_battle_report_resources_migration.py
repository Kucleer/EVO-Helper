"""`battle_report_resources` 那张表的迁移必须能升能降。

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸。这里两边对着比一遍。

⚠️ **方言取决于跑在哪**：设了 `EVO_HELPER_TEST_DATABASE_URL`（CI 上就是）时这几条
跑在真 Postgres 上，不设时仍是 SQLite。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from support.database import scratch_database_url

REVISION = "c4e8b2f70a15"
DOWN_REVISION = "b3d7e5a91c04"
TABLE = "battle_report_resources"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "migration.db")


def test_upgrade_creates_the_table(database_url: str) -> None:
    command.upgrade(_config(database_url), "head")

    inspector = inspect(create_engine(database_url))
    assert TABLE in inspector.get_table_names()
    columns = {column["name"]: column for column in inspector.get_columns(TABLE)}
    assert set(columns) == {"id", "report_id", "slot", "amount", "approximate", "uncertainty"}
    # 一列都不许可空：这张表的每一行都是一次读到的观测，缺了任何一样都无从解释。
    assert all(column["nullable"] is False for column in columns.values())


def test_the_slot_is_unique_per_report(database_url: str) -> None:
    """一份战报的一个格子只能有一行。

    没有这条约束，重复入库会在库里攒出两份收获——「这一发捞了多少」就变成一个
    要靠去重才答得出的问题。
    """
    command.upgrade(_config(database_url), "head")

    constraints = inspect(create_engine(database_url)).get_unique_constraints(TABLE)
    assert any(sorted(item["column_names"]) == ["report_id", "slot"] for item in constraints)


def test_downgrade_drops_the_table(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    inspector = inspect(create_engine(database_url))
    assert TABLE not in inspector.get_table_names()
    # 战报本身一动不动：这条迁移只加一张旁路表。
    assert "battle_reports" in inspector.get_table_names()


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    assert TABLE in inspect(create_engine(database_url)).get_table_names()


def test_the_migration_matches_the_orm_model(database_url: str, tmp_path: Path) -> None:
    """迁移建出来的列，和 `create_all` 建出来的必须一模一样。

    分叉了不会有人报错——测试库走 `create_all`，真库走迁移，两边各自都对。
    """
    from evo_helper.storage.database import Base

    command.upgrade(_config(database_url), "head")
    migrated = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(create_engine(database_url)).get_columns(TABLE)
    }

    # 另开一个库：两份表结构必须互不干扰，否则后建那次 `checkfirst` 会直接跳过，
    # 比出来永远相等。
    orm_engine = create_engine(scratch_database_url(tmp_path, "orm.db"))
    Base.metadata.create_all(orm_engine)
    created = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(orm_engine).get_columns(TABLE)
    }

    assert migrated == created
