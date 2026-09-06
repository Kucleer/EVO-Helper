"""一次性逐屏语料采样请求那张表的迁移。**当前的 head。**

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
from sqlalchemy import create_engine, inspect, text

from alembic import command
from support.database import scratch_database_url

REVISION = "b3e8f1a26d47"
DOWN_REVISION = "f2c04b8ae153"
TABLE = "ranking_capture_requests"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "capture-requests-migration.db")


def _columns(database_url: str) -> dict[str, dict[str, object]]:
    return {
        column["name"]: column for column in inspect(create_engine(database_url)).get_columns(TABLE)
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
    assert script.get_revision(REVISION).down_revision == DOWN_REVISION


def test_the_two_consumption_columns_are_nullable_with_no_default(database_url: str) -> None:
    """⚠️⚠️ **`consumed_at_utc` 必须可空、且不给默认值。**

    `NULL` = 「还没被消费」，而不是某个具体时刻。给个 `server_default` 会把每一行都
    摆成「已经录过了」，那样这张表一建出来就是空的语义 —— 而领取语句正是拿这一列
    当条件（`WHERE consumed_at_utc IS NULL`）。

    `consumed_by_run_id` 同理：它要回答「这批语料是哪次请求采的」，默认值会让这个
    问题永远答错。
    """
    command.upgrade(_config(database_url), "head")

    columns = _columns(database_url)
    for name in ("consumed_at_utc", "consumed_by_run_id"):
        assert columns[name]["nullable"] is True, name
        assert columns[name]["default"] is None, name


def test_the_request_time_is_not_null(database_url: str) -> None:
    """请求时刻非空 —— 「不知道什么时候点的」对这张表没有意义。"""
    command.upgrade(_config(database_url), "head")

    assert _columns(database_url)["requested_at_utc"]["nullable"] is False


def test_a_pending_request_is_claimed_exactly_once(database_url: str) -> None:
    """⚠️⚠️ **领取是一条原子语句，所以第二次领取必须什么都领不到。**

    这张表存在的全部理由就是「只被消费一次」。用两条 `UPDATE` 走一遍：
    第一条应该改到那一行，第二条应该改到 0 行。

    ⚠️ 这里刻意**直接发 SQL** 而不走仓储：验的是那一列的语义与索引，
    而不是仓储方法的接线（那一层有它自己的用例）。
    """
    command.upgrade(_config(database_url), "head")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"INSERT INTO {TABLE} (id, requested_at_utc)"
                " VALUES ('11111111-1111-1111-1111-111111111111', :now)"
            ),
            {"now": "2026-09-06 12:00:00+00"},
        )

    claim = text(
        f"UPDATE {TABLE} SET consumed_at_utc = :now,"
        " consumed_by_run_id = '22222222-2222-2222-2222-222222222222'"
        " WHERE consumed_at_utc IS NULL"
    )
    with engine.begin() as connection:
        first = connection.execute(claim, {"now": "2026-09-06 12:00:01+00"}).rowcount
    with engine.begin() as connection:
        second = connection.execute(claim, {"now": "2026-09-06 12:00:02+00"}).rowcount

    assert first == 1, "第一次领取应该改到那一行"
    assert second == 0, "第二次领取必须什么都领不到 —— 否则这张表白建"


def test_downgrade_removes_the_table(database_url: str) -> None:
    """回退把表整个删掉，不留残迹。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    assert TABLE in inspect(create_engine(database_url)).get_table_names()

    command.downgrade(config, "-1")

    assert TABLE not in inspect(create_engine(database_url)).get_table_names()


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """回退再升一次要能跑通 —— 索引名重复是这类迁移最常见的绊脚石。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, "-1")
    command.upgrade(config, "head")

    assert TABLE in inspect(create_engine(database_url)).get_table_names()
