"""「那个军力值是不是插出来的」这一列的迁移：必须可空，且不给默认值。

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸。这里两边对着比一遍。

⚠️ **方言取决于跑在哪**：设了 `EVO_HELPER_TEST_DATABASE_URL`（CI 上就是）时这几条
跑在真 Postgres 上，不设时仍是 SQLite。

⚠️ **单一 head 单独钉一条。** 生产的升级机制就是启动时 `alembic upgrade head`
（`web.runtime._upgrade_database`）——多出一个 head，用户重启 bat 之后控制台直接
报「Multiple head revisions」起不来，而这件事在合并之前一个字都看不出来。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command
from support.database import scratch_database_url

REVISION = "e2a7c15b9d40"
DOWN_REVISION = "c3f7a2b81d54"
TABLE = "attack_intents"
ESTIMATED = "target_military_score_estimated"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "military-estimated-migration.db")


def _columns(database_url: str) -> dict[str, dict[str, object]]:
    return {
        column["name"]: column for column in inspect(create_engine(database_url)).get_columns(TABLE)
    }


def test_the_history_has_exactly_one_head() -> None:
    """整条迁移链只有一个 head，而且这一条在通往它的路上。

    生产靠启动时 `alembic upgrade head` 自升，多一个 head 就是直接起不来。

    ⚠️ **这里不再写死 head 的名字。** 这一条原本断言 head 就是 `e2a7c15b9d40`
    本身，于是后面每接一条迁移都会把它撞红——而撞红的原因是「链条正常往前走了」，
    不是「多了一个 head」，读起来正好相反（`test_attack_intent_military_snapshot_migration`
    上一次就是这么被迫改的）。head 的身份由**最新那条迁移**自己的用例钉；
    这一条只管两件不随时间变的事：head 唯一，且这条迁移仍在链上。
    """
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert len(script.get_heads()) == 1
    head = script.get_heads()[0]
    chain = {revision.revision for revision in script.iterate_revisions(head, "base")}
    assert REVISION in chain, "这条迁移从链上掉下来了——真库升到 head 也不会有那一列"


def test_the_new_column_is_nullable_with_no_default(database_url: str) -> None:
    """**必须可空，而且一个默认值都不许给。**

    这一列上 `False` 的含义是「这个数是实读的」。存量意图根本不知道当时那个数
    是怎么来的——给 `server_default='0'`（或任何非空默认）会把整库的历史派遣
    一次性标成实读，而这个标记存在的全部意义正是把插值和实读分开。一旦回填，
    再也分不出哪几行是真的读到过。

    NULL 才是实话：「当时没记这件事」。页面据此既不标「(估算)」，也不反过来
    声称实读。
    """
    command.upgrade(_config(database_url), "head")

    columns = _columns(database_url)
    assert ESTIMATED in columns
    assert columns[ESTIMATED]["nullable"] is True
    assert columns[ESTIMATED]["default"] is None, "这一列被塞了默认值——历史行会集体冒充实读"
    # #183 那两列一个都没动：这条迁移只加列。
    assert {"target_military_score", "target_military_score_at_utc"} <= set(columns)


def test_downgrade_removes_only_the_new_column(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    columns = set(_columns(database_url))
    assert ESTIMATED not in columns
    # 退回来之后 #183 的快照还在：这条迁移不该把上一条的成果一起带走。
    assert {"target_military_score", "target_military_score_at_utc"} <= columns


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    assert ESTIMATED in _columns(database_url)


def test_the_migration_matches_the_orm_model(database_url: str, tmp_path: Path) -> None:
    """迁移建出来的列，和 `create_all` 建出来的必须一模一样。

    分叉了不会有人报错——测试库走 `create_all`，真库走迁移，两边各自都对。
    这一条同时守着「可空」这件事的另一半：只把 ORM 改成非空、迁移不动（或者
    反过来），上面那条照样绿，而真库和测试库已经是两个 schema 了。
    """
    from evo_helper.storage.database import Base

    command.upgrade(_config(database_url), "head")
    migrated = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(create_engine(database_url)).get_columns(TABLE)
    }

    # 另开一个库：迁移建的那份和 `create_all` 建的那份必须互不干扰，
    # 不然后建的那次 `checkfirst` 会直接跳过，比出来永远相等。
    orm_engine = create_engine(scratch_database_url(tmp_path, "orm-estimated.db"))
    Base.metadata.create_all(orm_engine)
    created = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(orm_engine).get_columns(TABLE)
    }

    assert migrated == created
