"""`mission_tasks` / `mission_runs` 的迁移与模型必须是同一份结构。

**跑测试的那条路建表用的是 `Base.metadata.create_all`，跑生产的那条路用的是
alembic。** 两条路各建各的，一整套测试可以全绿，而真正的库上少一列——那一列
在 ORM 里有、在库里没有，第一次读写就是 `no such column`，而它只会在实机上发生。

所以这里把两条路各建一次，逐列对比。⚠️ 这条**不是**在重测「迁移能不能跑通」，
往返（upgrade → downgrade → upgrade）与既有取值原样保留那部分是在生产库副本上
人工验的，见 PR 说明。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, inspect

from alembic import command
from evo_helper.storage.database import Base, create_database_engine

#: 本轮之后这两张表长什么样，逐条写死。**不从 metadata 反推**：反推出来的清单
#: 会跟着模型一起变，那样这条断言永远成立，也就永远不告诉你任何事。
EXPECTED_MISSION_TASK_COLUMNS = frozenset(
    {
        "id",
        "kind",
        "name",
        "enabled",
        "priority",
        "params_json",
        "origin_galaxy",
        "origin_system",
        "origin_position",
        "fleet_lines",
        "round_started_at_utc",
        "quota_exhausted_until_utc",
        "consecutive_failures",
        "disabled_reason",
        "created_at_utc",
        "updated_at_utc",
    }
)

EXPECTED_MISSION_RUN_COLUMNS = frozenset(
    {
        "id",
        "kind",
        "task_id",
        "command",
        "pid",
        "started_at_utc",
        "ended_at_utc",
        "exit_code",
        "stopped_by",
        "log_path",
    }
)


@pytest.fixture
def migrated(tmp_path: Path) -> Iterator[Engine]:
    """一个只由 alembic 建出来的库——生产走的就是这条路。"""
    url = f"sqlite:///{tmp_path / 'migrated.db'}"
    config = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def from_metadata(tmp_path: Path) -> Iterator[Engine]:
    """一个只由 `Base.metadata` 建出来的库——测试走的就是这条路。"""
    engine = create_database_engine(f"sqlite:///{tmp_path / 'metadata.db'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


def _columns(engine: Engine, table: str) -> frozenset[str]:
    return frozenset(column["name"] for column in inspect(engine).get_columns(table))


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("mission_tasks", EXPECTED_MISSION_TASK_COLUMNS),
        ("mission_runs", EXPECTED_MISSION_RUN_COLUMNS),
    ],
)
def test_the_migration_and_the_model_build_the_same_columns(
    migrated: Engine, from_metadata: Engine, table: str, expected: frozenset[str]
) -> None:
    assert _columns(migrated, table) == expected
    assert _columns(from_metadata, table) == expected


def test_two_tasks_of_the_same_kind_fit_in_the_migrated_schema(migrated: Engine) -> None:
    """**`kind` 上那道唯一约束必须真的没了。**

    它是 `e29d06f8489f` 用匿名 `UniqueConstraint("kind")` 建的，SQLite 里按名字
    删不掉——迁移靠「用一份不含它的表定义重建」把它去掉，而那件事只有在真的往里
    插第二行 BOT 时才看得出来做成没有。`Base.metadata` 那条路早就没有它了，
    所以光比列名是发现不了的。
    """
    from datetime import UTC, datetime

    from sqlalchemy import text

    now = datetime.now(UTC).isoformat()
    with migrated.begin() as connection:
        for name in ("主星", "2 号星"):
            connection.execute(
                text(
                    "INSERT INTO mission_tasks "
                    "(kind, name, enabled, priority, params_json, consecutive_failures, "
                    " created_at_utc, updated_at_utc) "
                    "VALUES ('BOT', :name, 0, 1, '{}', 0, :now, :now)"
                ),
                {"name": name, "now": now},
            )
        count = connection.execute(
            text("SELECT COUNT(*) FROM mission_tasks WHERE kind = 'BOT'")
        ).scalar()

    assert count == 2
