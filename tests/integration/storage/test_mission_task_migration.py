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
from sqlalchemy import Engine, create_engine, inspect

from alembic import command
from evo_helper.storage.database import Base, create_database_engine
from support.database import scratch_database_url

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
        "enabled_from_utc",
        "enabled_until_utc",
        "round_started_at_utc",
        "quota_exhausted_until_utc",
        "consecutive_failures",
        "disabled_reason",
        "disabled_recovery",
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
    url = scratch_database_url(tmp_path, "migrated.db")
    config = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    engine = create_database_engine(url)
    yield engine
    engine.dispose()


@pytest.fixture
def from_metadata(tmp_path: Path) -> Iterator[Engine]:
    """一个只由 `Base.metadata` 建出来的库——测试走的就是这条路。"""
    engine = create_database_engine(scratch_database_url(tmp_path, "metadata.db"))
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

    ⚠️ `enabled` 与两个时刻走**带类型的绑定参数**，不写成字面量。SQLite 拿 `0` 当
    假、拿 ISO 串当时刻都没意见；Postgres 上 `enabled` 是 `boolean`、时刻是
    `timestamptz`，塞整数和文本进去直接 `DatatypeMismatch`。带上类型之后，两种方言
    各自渲染各自认的字面量。
    """
    from datetime import UTC, datetime

    from sqlalchemy import Boolean, DateTime, bindparam, text

    insert = text(
        "INSERT INTO mission_tasks "
        "(kind, name, enabled, priority, params_json, consecutive_failures, "
        " created_at_utc, updated_at_utc) "
        "VALUES ('BOT', :name, :enabled, 1, '{}', 0, :now, :now)"
    ).bindparams(
        bindparam("enabled", type_=Boolean()),
        bindparam("now", type_=DateTime(timezone=True)),
    )
    now = datetime.now(UTC)
    with migrated.begin() as connection:
        for name in ("主星", "2 号星"):
            connection.execute(insert, {"name": name, "enabled": False, "now": now})
        count = connection.execute(
            text("SELECT COUNT(*) FROM mission_tasks WHERE kind = 'BOT'")
        ).scalar()

    assert count == 2


#: 定时开关那条迁移（`b3f5c8d10a27`）的上一格。降到这里，那两列就该没了。
SCHEDULE_WINDOW_DOWN_REVISION = "a7f2c9d40b16"


def _schedule_window_config(tmp_path: Path) -> tuple[Config, str]:
    url = f"sqlite:///{tmp_path / 'window.db'}"
    config = Config(str(Path(__file__).resolve().parents[3] / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    return config, url


def test_the_schedule_window_migration_can_be_rolled_back_and_replayed(tmp_path: Path) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死，
    而那正是升上去发现不对时唯一的退路。
    """
    config, url = _schedule_window_config(tmp_path)
    window_columns = {"enabled_from_utc", "enabled_until_utc"}
    command.upgrade(config, "head")
    engine = create_engine(url)
    assert window_columns <= _columns(engine, "mission_tasks")

    command.downgrade(config, SCHEDULE_WINDOW_DOWN_REVISION)
    assert not (window_columns & _columns(engine, "mission_tasks"))

    command.upgrade(config, "head")
    assert window_columns <= _columns(engine, "mission_tasks")
    engine.dispose()


def test_existing_tasks_come_out_of_the_migration_with_no_window(tmp_path: Path) -> None:
    """升级完成的那一刻，既有任务的两列都是 NULL。

    给它们一个默认窗口——哪怕只是「今天 00:00 起」——就等于把「两列都为空 =
    行为完全不变」这条承诺反过来：升完级之后，某个没人配过的时刻会把任务停掉。
    """
    from datetime import UTC, datetime

    from sqlalchemy import text

    config, url = _schedule_window_config(tmp_path)
    command.upgrade(config, SCHEDULE_WINDOW_DOWN_REVISION)
    engine = create_engine(url)
    now = datetime.now(UTC).isoformat()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mission_tasks "
                "(kind, name, enabled, priority, params_json, consecutive_failures, "
                " created_at_utc, updated_at_utc) "
                "VALUES ('BOT', '升级前就在的任务', 1, 1, '{}', 0, :now, :now)"
            ),
            {"now": now},
        )

    command.upgrade(config, "head")

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT enabled, enabled_from_utc, enabled_until_utc "
                "FROM mission_tasks WHERE name = '升级前就在的任务'"
            )
        ).one()
    # `enabled` 也一起量：迁移顺手改用户那一列，正是这整个特性最不能出的错。
    assert row == (1, None, None)
    engine.dispose()
