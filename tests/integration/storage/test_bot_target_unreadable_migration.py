"""「面板名读不出」那三列的迁移：可空/默认值三者各不相同，而且它就是当前的 head。

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

REVISION = "d4b6e0f19c73"
DOWN_REVISION = "61eb261c5a09"
TARGETS = "bot_targets"
SEEN_AT = "unreadable_seen_at_utc"
ATTEMPTS = "unreadable_attempts"
CONFIG = "military_attack_config"
EXCLUSION = "unreadable_exclusion_hours"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "unreadable-panel-migration.db")


def _columns(database_url: str, table: str) -> dict[str, dict[str, object]]:
    return {
        column["name"]: column for column in inspect(create_engine(database_url)).get_columns(table)
    }


def test_this_revision_is_the_head() -> None:
    """**这一条是最新的迁移，所以由它来说「head 就是我」。**

    生产靠启动时 `alembic upgrade head` 自升（`web.runtime._upgrade_database`），
    多一个 head 就是用户重启 bat 之后控制台直接起不来——而这件事在合并之前一个字
    都看不出来。合并时若发现别的分支也挂在 `61eb261c5a09` 上，**后进的那条改自己
    的 `down_revision`**。

    ⚠️ 再往后接新迁移时，把这两句挪到那一条自己的用例里，并把这里降级成
    「链上只有一个 head，而我在链上」——先例见
    `test_dispatch_flight_source_migration.py`。
    """
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert script.get_heads() == [REVISION]


def test_the_timestamp_and_the_knob_are_nullable_with_no_default(database_url: str) -> None:
    """**时刻列和旋钮列都必须可空，而且一个默认值都不许给。**

    - `unreadable_seen_at_utc` 的 NULL 是「从没读不出过」。给任何具体时刻做默认值，
      全库存量目标会被一次性排除掉——一夜没活干，而页面上一切正常。
    - `unreadable_exclusion_hours` 的 NULL 是「跟着代码里的默认值走」，这正是升级
      完成那一刻行为完全不变的保证；写死当时的取值，日后调默认值它不跟。
    """
    command.upgrade(_config(database_url), "head")

    targets = _columns(database_url, TARGETS)
    assert SEEN_AT in targets
    assert targets[SEEN_AT]["nullable"] is True
    assert targets[SEEN_AT]["default"] is None, "存量目标会被这个默认值集体排除掉"

    config = _columns(database_url, CONFIG)
    assert EXCLUSION in config
    assert config[EXCLUSION]["nullable"] is True
    assert config[EXCLUSION]["default"] is None, "有了默认值，日后调代码里的默认值它不跟"


def test_the_counter_is_not_null_and_starts_at_zero(database_url: str) -> None:
    """**计数列反过来：非空、默认 0。**

    和上面两列处理不同，不是疏漏。0 对存量行是**真话**（「还没失败过」），而时刻列
    没有一个诚实的默认时刻可给。可空的话，「连续第 N 次」的算式每次都要先 `or 0`，
    而漏一处就会把 `None + 1` 抛成异常——抛在 runner 的善后路径上，正好是最不该
    出事的地方。
    """
    command.upgrade(_config(database_url), "head")

    targets = _columns(database_url, TARGETS)
    assert ATTEMPTS in targets
    assert targets[ATTEMPTS]["nullable"] is False
    assert str(targets[ATTEMPTS]["default"]).strip("'\"") == "0"


def test_downgrade_removes_only_the_new_columns(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    assert SEEN_AT not in _columns(database_url, TARGETS)
    assert ATTEMPTS not in _columns(database_url, TARGETS)
    assert EXCLUSION not in _columns(database_url, CONFIG)
    # 上一条迁移的成果还在：这条不该把别人的列一起带走。
    assert "protection_seen_at_utc" in _columns(database_url, TARGETS)
    assert "protection_exclusion_hours" in _columns(database_url, CONFIG)


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    assert SEEN_AT in _columns(database_url, TARGETS)
    assert ATTEMPTS in _columns(database_url, TARGETS)
    assert EXCLUSION in _columns(database_url, CONFIG)


@pytest.mark.parametrize("table", [TARGETS, CONFIG])
def test_the_migration_matches_the_orm_model(database_url: str, tmp_path: Path, table: str) -> None:
    """迁移建出来的列，和 `create_all` 建出来的必须一模一样。

    分叉了不会有人报错——测试库走 `create_all`，真库走迁移，两边各自都对。
    这一条同时守着时刻列那件最容易错的事：`UTCDateTime` 的 `impl` 是
    `DateTime(timezone=True)`，迁移里写成不带时区的话，Postgres 上 tzinfo 会被
    **静默截掉**，读回来变成 naive——而这个仓所有的判据都建立在「读出来是 aware
    的 UTC」上（`storage.database.UTCDateTime` 记着已经被坑过三次）。
    """
    from evo_helper.storage.database import Base

    command.upgrade(_config(database_url), "head")
    migrated = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(create_engine(database_url)).get_columns(table)
    }

    # 另开一个库：迁移建的那份和 `create_all` 建的那份必须互不干扰，
    # 不然后建的那次 `checkfirst` 会直接跳过，比出来永远相等。
    orm_engine = create_engine(scratch_database_url(tmp_path, "orm-unreadable.db"))
    Base.metadata.create_all(orm_engine)
    created = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(orm_engine).get_columns(table)
    }

    assert migrated == created
