"""「每轮配着几条航线」那一列与挂机心跳那张表的迁移，而且它就是当前的 head。

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸。这里两边对着比一遍。

⚠️ **方言取决于跑在哪**：设了 `EVO_HELPER_TEST_DATABASE_URL`（CI 上就是）时这几条
跑在真 Postgres 上，不设时仍是 SQLite。

⚠️ **这条迁移一次都没有在任何真实库上执行过。** 生产自己在启动时升，开发一侧
不碰（CLAUDE.md 的硬约束）；这份用例跑的全是临时库。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command
from support.database import scratch_database_url

REVISION = "a3c81f5d2b64"
DOWN_REVISION = "d4b6e0f19c73"
RUNS = "mission_runs"
LINES = "configured_lines"
UPTIME = "scheduler_uptime_segments"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "run-lines-uptime-migration.db")


def _columns(database_url: str, table: str) -> dict[str, dict[str, object]]:
    return {
        column["name"]: column for column in inspect(create_engine(database_url)).get_columns(table)
    }


def test_this_revision_is_on_a_single_headed_chain() -> None:
    """链上只有一个 head，而这一条在链上。

    生产靠启动时 `alembic upgrade head` 自升（`web.runtime._upgrade_database`），
    多一个 head 就是用户重启 bat 之后控制台直接起不来——而这件事在合并之前一个字
    都看不出来。合并时若发现别的分支也挂在同一个父节点上，**后进的那条改自己的
    `down_revision`**。

    ⚠️ 这条**不再断言「head 就是我」**：后面又接了新的迁移（`b8e1c4a72f05`，
    盲滚行数），「谁是 head」这句话只该由**最新那一条**的用例来说，否则每加一条
    迁移都要回来改一次这里，而改多了就没人再当真（同
    `test_dispatch_flight_source_migration.py` 与
    `test_bot_target_unreadable_migration.py` 里那一段的理由）。
    """
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert len(script.get_heads()) == 1
    assert REVISION in {revision.revision for revision in script.walk_revisions()}


def test_the_line_count_is_nullable_with_no_default(database_url: str) -> None:
    """⚠️ **可空，而且一个默认值都不许给。NULL 的意思是「不知道」。**

    - 给 `server_default="0"` 会让存量行看起来「配了 0 条」，那些天的分母因此变成
      0、利用率整段显示成「—」，把真打出去的活抹掉；
    - 回填「此刻配着几条」更糟：用户 2026-08-20 当天把航线从 4 条加到 9 条，
      按 9 条去算 08-15（当时 4 条）会把那天低估到 44%，**而页面上一点异样都
      看不出来**。

    NULL 的那些天改用「当天最大并发在飞数」当下界，页面上会标「≤」。
    """
    command.upgrade(_config(database_url), "head")

    runs = _columns(database_url, RUNS)
    assert LINES in runs
    assert runs[LINES]["nullable"] is True
    assert runs[LINES]["default"] is None, "有了默认值，存量行就会冒充一个真读数"


def test_the_uptime_table_has_no_end_column_only_a_last_beat(database_url: str) -> None:
    """⚠️ **刻意没有「结束时刻」这一列。**

    进程被杀、断电、任务管理器结束进程时，不会有人来写它。「最后一拍」天然就是
    这一段的右端，所以挂机时长在崩溃之后不会继续涨。加一列 `ended_at_utc` 就等于
    把「谁来写它」这个没有答案的问题又请回来了。
    """
    command.upgrade(_config(database_url), "head")

    uptime = _columns(database_url, UPTIME)
    assert set(uptime) == {"id", "started_at_utc", "last_beat_at_utc"}
    assert uptime["started_at_utc"]["nullable"] is False
    assert uptime["last_beat_at_utc"]["nullable"] is False


def test_downgrade_removes_only_what_this_revision_added(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    assert LINES not in _columns(database_url, RUNS)
    assert UPTIME not in inspect(create_engine(database_url)).get_table_names()
    # 别人的成果还在：这条不该把上一条迁移的列一起带走。
    assert "unreadable_attempts" in _columns(database_url, "bot_targets")
    assert "task_id" in _columns(database_url, RUNS)


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    assert LINES in _columns(database_url, RUNS)
    assert UPTIME in inspect(create_engine(database_url)).get_table_names()


@pytest.mark.parametrize("table", [RUNS, UPTIME])
def test_the_migration_matches_the_orm_model(database_url: str, tmp_path: Path, table: str) -> None:
    """迁移建出来的列，和 `create_all` 建出来的必须一模一样。

    分叉了不会有人报错——测试库走 `create_all`，真库走迁移，两边各自都对。
    这一条同时守着时刻列那件最容易错的事：`UTCDateTime` 的 `impl` 是
    `DateTime(timezone=True)`，迁移里写成不带时区的话，Postgres 上 tzinfo 会被
    **静默截掉**，读回来变成 naive——而这个仓所有的判据都建立在「读出来是 aware
    的 UTC」上（挂机时长与航线占用全都在比时刻）。
    """
    from evo_helper.storage.database import Base

    command.upgrade(_config(database_url), "head")
    migrated = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(create_engine(database_url)).get_columns(table)
    }

    # 另开一个库：迁移建的那份和 `create_all` 建的那份必须互不干扰，
    # 不然后建的那次 `checkfirst` 会直接跳过，比出来永远相等。
    orm_engine = create_engine(scratch_database_url(tmp_path, "orm-run-lines-uptime.db"))
    Base.metadata.create_all(orm_engine)
    created = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(orm_engine).get_columns(table)
    }

    assert migrated == created
