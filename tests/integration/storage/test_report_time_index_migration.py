"""给 `battle_reports.reported_at_utc` 加索引的那条迁移。**当前的 head。**

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸。**索引这一类分叉起来格外没有声音**：少一个索引不报错、
不缺数、页面上什么都看不出来，只是那句每封邮件都要问一次的去重探针
（`storage.repository.has_report_at`）退回整表扫。所以这里把模型和迁移对着比一遍。

⚠️ **方言取决于跑在哪**：设了 `EVO_HELPER_TEST_DATABASE_URL`（CI 上就是）时这几条
跑在真 Postgres 上，不设时仍是 SQLite。`CREATE INDEX` 两边都原生支持，所以这条迁移
没走 `batch_alter_table` —— 这一点由下面
`test_upgrade_is_replayable_after_a_downgrade` 兜着。

⚠️ **这条迁移一次都没有在任何真实库上执行过。** 生产自己在启动时升，开发一侧不碰
（CLAUDE.md 的硬约束）；这份用例跑的全是临时库。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command
from evo_helper.storage.models import BattleReportRow
from support.database import scratch_database_url

REVISION = "d3b9f27c4a81"
DOWN_REVISION = "c7d92f4a1b60"
TABLE = "battle_reports"
COLUMN = "reported_at_utc"
INDEX = "ix_battle_reports_reported_at_utc"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "report-time-index-migration.db")


def _indexes(database_url: str) -> dict[str, list[str]]:
    return {
        index["name"]: list(index["column_names"])
        for index in inspect(create_engine(database_url)).get_indexes(TABLE)
        if index["name"]
    }


def test_this_revision_is_the_single_head() -> None:
    """链上只有一个 head，而且就是这一条。

    生产靠启动时 `alembic upgrade head` 自升（`web.runtime._upgrade_database`），
    多一个 head 就是用户重启 bat 之后控制台直接起不来——而这件事在合并之前一个字
    都看不出来。

    ⚠️ 「head 就是我」这句话只有**最新那一条**该说；等下一条迁移接上来，这里要跟着
    退回成「我在链上」（同 `test_drop_blind_scrolls_migration.py` 里那一段）。
    """
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert list(script.get_heads()) == [REVISION]
    assert script.get_revision(REVISION).down_revision == DOWN_REVISION


def test_the_index_is_there_after_upgrade(database_url: str) -> None:
    """升完之后索引在，而且盖的正是那一列。

    只断言名字不够：名字对、列错的索引照样让去重探针整表扫，而这种错和「忘了加」
    在页面上是同一个症状（什么都看不出来，只是慢）。
    """
    command.upgrade(_config(database_url), "head")

    assert _indexes(database_url).get(INDEX) == [COLUMN]


def test_the_model_declares_the_same_index(database_url: str) -> None:
    """⚠️⚠️ **模型和迁移必须指同一条索引。**

    这是索引这一类唯一会静默分叉的地方：本地建表走 `create_all`，索引按模型来；
    生产建表走迁移。两边名字或列对不上，本地全绿，而生产那张真表上就是没有这条
    索引——直到有人去量为什么那句探针慢。
    """
    command.upgrade(_config(database_url), "head")

    declared = {
        index.name: [column.name for column in index.columns]
        for index in BattleReportRow.__table__.indexes
    }

    assert declared.get(INDEX) == [COLUMN]
    assert _indexes(database_url).get(INDEX) == declared[INDEX]


def test_downgrade_removes_the_index(database_url: str) -> None:
    """回退把索引删掉，一行数据都不动（索引不承载语义，退回去只是查询变慢）。

    ⚠️ **回退目标写 `DOWN_REVISION` 而不是 `-1`**：等下一条迁移接上来，`-1` 从 head
    往回只走一步，落在本条之后那一条上，于是这里会为一个和本条无关的原因红
    （`test_ranking_capture_requests_migration.py` 就是这么红过一次的）。
    """
    config = _config(database_url)
    command.upgrade(config, "head")
    assert INDEX in _indexes(database_url)

    command.downgrade(config, DOWN_REVISION)

    assert INDEX not in _indexes(database_url)


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """回退再升一次要能跑通 —— 索引名重复是这类迁移最常见的绊脚石。

    ⚠️ 这一条同时是「`CREATE INDEX` / `DROP INDEX` 在这个方言上真的能跑」的证据：
    走不通的环境下这里会红，而不是等到生产升级时才炸。
    """
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)
    command.upgrade(config, "head")

    assert _indexes(database_url).get(INDEX) == [COLUMN]
