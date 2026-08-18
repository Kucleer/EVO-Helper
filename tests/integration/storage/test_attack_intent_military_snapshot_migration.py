"""「派遣时看到的目标军力」那两列的迁移必须能升能降，且保持单一 head。

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

REVISION = "c3f7a2b81d54"
DOWN_REVISION = "b1d9e47f2a03"
TABLE = "attack_intents"
SCORE = "target_military_score"
OBSERVED_AT = "target_military_score_at_utc"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "military-snapshot-migration.db")


def test_the_history_has_exactly_one_head_and_this_revision_is_on_it() -> None:
    """整条迁移链只有一个 head，而且这一条在通往它的路上。

    生产靠启动时 `alembic upgrade head` 自升，多一个 head 就是直接起不来。

    ⚠️ **这里不再写死 head 的名字。** 这一条原本断言 head 就是 `c3f7a2b81d54`
    本身，于是后面每接一条迁移都会把它撞红——而撞红的原因是「链条正常往前走了」，
    不是「多了一个 head」，读起来正好相反。head 的身份由**最新那条迁移**自己的
    用例钉（现在是 `test_attack_intent_military_estimated_migration`）；这一条
    只管两件不随时间变的事：head 唯一，且这条迁移仍在链上。
    """
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert len(script.get_heads()) == 1
    head = script.get_heads()[0]
    chain = {revision.revision for revision in script.iterate_revisions(head, "base")}
    assert REVISION in chain, "这条迁移从链上掉下来了——真库升到 head 也不会有那两列"


def test_upgrade_adds_two_nullable_columns(database_url: str) -> None:
    """两列都必须可空。

    存量意图没有这个快照，NULL 就是「当时没记」。给非空默认值（哪怕是 0）会把
    整库的历史派遣一次性标成「当时看到的军力是 0」——而军力 0 是一个合法读数
    （被打空的 bot），于是编造出来的值和真实观测再也分不开。
    """
    command.upgrade(_config(database_url), "head")

    columns = {
        column["name"]: column for column in inspect(create_engine(database_url)).get_columns(TABLE)
    }
    assert columns[SCORE]["nullable"] is True
    assert columns[OBSERVED_AT]["nullable"] is True
    # 意图那几列一个都没动：这条迁移只加列。
    assert {"target_galaxy", "target_kind", "preset_name"} <= set(columns)


def test_downgrade_removes_both_columns(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    columns = {column["name"] for column in inspect(create_engine(database_url)).get_columns(TABLE)}
    assert SCORE not in columns
    assert OBSERVED_AT not in columns
    assert "target_kind" in columns


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    columns = {column["name"] for column in inspect(create_engine(database_url)).get_columns(TABLE)}
    assert {SCORE, OBSERVED_AT} <= columns
