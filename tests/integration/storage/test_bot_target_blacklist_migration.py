"""黑名单那两列的迁移。**当前的 head。**

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸。这里两边对着比一遍。

⚠️ **方言取决于跑在哪**：设了 `EVO_HELPER_TEST_DATABASE_URL`（CI 上就是）时这几条
跑在真 Postgres 上，不设时仍是 SQLite。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command
from support.database import scratch_database_url

REVISION = "a7d3e91c05b2"
DOWN_REVISION = "c7f2a91d4e08"
TARGETS = "bot_targets"
AT = "blacklisted_at_utc"
REASON = "blacklist_reason"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "blacklist-migration.db")


def _columns(database_url: str, table: str) -> dict[str, dict[str, object]]:
    return {
        column["name"]: column for column in inspect(create_engine(database_url)).get_columns(table)
    }


def test_this_revision_is_the_single_head() -> None:
    """链上只有一个 head，而且就是这一条。

    生产靠启动时 `alembic upgrade head` 自升（`web.runtime._upgrade_database`），
    多一个 head 就是用户重启 bat 之后控制台直接起不来——而这件事在合并之前一个字
    都看不出来。

    ⚠️ 「head 就是我」这句话只有**最新那一条**该说；等下一条迁移接上来，这里要跟着
    退回成「我在链上」（同 `test_bot_target_unreadable_migration.py` 里那一段）。
    """
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert list(script.get_heads()) == [REVISION]


def test_both_columns_are_nullable_with_no_default(database_url: str) -> None:
    """**两列都可空，一个默认值都不许给。**

    NULL = 「没拉黑」。给 `blacklisted_at_utc` 任何具体时刻做默认值，库里六千多行
    会被**一次性全拉黑**——一夜一发没派，而页面上一切正常（候选池会显示 0，
    看起来像「今晚没目标」）。
    """
    command.upgrade(_config(database_url), "head")

    targets = _columns(database_url, TARGETS)
    assert targets[AT]["nullable"] is True
    assert targets[AT]["default"] is None, "全库六千多行会被这个默认值一次性拉黑"
    assert targets[REASON]["nullable"] is True
    assert targets[REASON]["default"] is None


def test_downgrade_removes_only_the_new_columns(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    assert AT not in _columns(database_url, TARGETS)
    assert REASON not in _columns(database_url, TARGETS)
    # 前几条迁移的成果还在：这条不该把别人的列一起带走。
    assert "protection_seen_at_utc" in _columns(database_url, TARGETS)
    assert "unreadable_seen_at_utc" in _columns(database_url, TARGETS)
