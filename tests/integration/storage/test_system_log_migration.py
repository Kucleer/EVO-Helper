"""`system_log` 那条迁移必须能升能降，且建出来的表与 ORM 一致。

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸（`test_web_runtime` 的注释里记着同一条教训）。这里两边
对着比一遍。

**降级也跑**：升级链上任何一步不可逆，都会让「升上去发现不对、退回来」这条路
在真库上断掉，而那正是出问题时唯一的退路。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from support.database import scratch_database_url

REVISION = "a7f2c9d40b16"
DOWN_REVISION = "fa1c3d4e5f67"


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


def test_upgrade_creates_the_table_with_its_four_indexes(database_url: str) -> None:
    command.upgrade(_config(database_url), "head")

    inspector = inspect(create_engine(database_url))
    columns = {column["name"]: column for column in inspector.get_columns("system_log")}
    assert set(columns) == {
        "id",
        "logged_at_utc",
        "level",
        "source",
        "host",
        "pid",
        "run_id",
        "task_id",
        "mission_kind",
        "message",
        "payload_json",
    }
    # 刻意没有 `seq`：同一进程 FIFO 入库，`id` 已经是发生顺序。
    assert "seq" not in columns
    assert {index["name"] for index in inspector.get_indexes("system_log")} == {
        "ix_system_log_logged_at_id",
        "ix_system_log_run_id_id",
        "ix_system_log_host_logged_at",
        "ix_system_log_level_logged_at",
    }
    foreign_keys = inspector.get_foreign_keys("system_log")
    assert [key["referred_table"] for key in foreign_keys] == ["mission_runs"]
    # ⚠️ 不许是 CASCADE：一轮记录被清掉不该顺手删掉「那一轮发生了什么」。
    assert not (foreign_keys[0].get("options") or {}).get("ondelete")


def test_the_id_column_really_autoincrements_on_sqlite(database_url: str) -> None:
    """⚠️ 这条守的是两种方言的差别，实测（2026-08-16）出来的。

    纯 `BigInteger` 主键在 SQLite 上建成 `BIGINT`，而 SQLite 只把
    `INTEGER PRIMARY KEY` 当 rowid 别名——不带 id 的 insert 会当场
    `IntegrityError: NOT NULL constraint failed: system_log.id`。
    把 `with_variant(Integer, "sqlite")` 去掉，这条立刻红。
    """
    from sqlalchemy import text

    command.upgrade(_config(database_url), "head")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        for message in ("先", "后"):
            connection.execute(
                text(
                    "INSERT INTO system_log "
                    "(logged_at_utc, level, source, host, pid, message) "
                    "VALUES ('2026-08-16 00:00:00', 'INFO', 'tests', 'pc', 1, :m)"
                ),
                {"m": message},
            )
        ids = [row[0] for row in connection.execute(text("SELECT id FROM system_log ORDER BY id"))]

    assert ids == [1, 2]


def test_downgrade_removes_the_table(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    assert "system_log" not in inspect(create_engine(database_url)).get_table_names()


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    assert "system_log" in inspect(create_engine(database_url)).get_table_names()


def test_the_migration_matches_the_orm_model(database_url: str) -> None:
    """迁移建出来的列，和 `create_all` 建出来的必须一模一样。

    分叉了不会有人报错——测试库走 `create_all`，真库走迁移，两边各自都对。
    """
    from evo_helper.storage.database import Base

    command.upgrade(_config(database_url), "head")
    migrated = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(create_engine(database_url)).get_columns("system_log")
    }

    orm_url = database_url.replace("migration.db", "orm.db")
    orm_engine = create_engine(orm_url)
    Base.metadata.create_all(orm_engine)
    created = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(orm_engine).get_columns("system_log")
    }

    assert migrated == created
