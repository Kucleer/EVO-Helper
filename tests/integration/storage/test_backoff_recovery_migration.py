"""退避自动恢复那两列的迁移。**当前的 head。**

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

REVISION = "f2c04b8ae153"
DOWN_REVISION = "a7d3e91c05b2"
TASKS = "mission_tasks"
RETRY_AFTER = "retry_after_utc"
ROUNDS = "backoff_rounds"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "backoff-migration.db")


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


def test_the_alarm_is_nullable_with_no_default(database_url: str) -> None:
    """**`retry_after_utc` 必须可空，而且一个默认值都不许给。**

    NULL = 「没有在等冷却」，不是某个具体时刻。给它任何默认时刻，升级那一下库里
    每一行都会摆成「随时可以自动打开」——**包括用户自己关掉的那四个任务**
    （侦查+攻击海盗、扫描全星系 bot、5 系攻击、9 系攻击）。那是这次改动唯一
    不能出的错。

    本列上线之前的历史行同样是 NULL，语义正好：调度器没给它们定过重试时刻。
    """
    command.upgrade(_config(database_url), "head")

    tasks = _columns(database_url, TASKS)
    assert tasks[RETRY_AFTER]["nullable"] is True
    assert tasks[RETRY_AFTER]["default"] is None


def test_the_round_counter_is_not_null_and_starts_at_zero(database_url: str) -> None:
    """**`backoff_rounds` 非空、默认 0，两件事都是必须的。**

    非空：它是个**计数**，「不知道」这个状态对它没有意义。留成可空的话，每一处
    读它的地方都要写一遍 `or 0`，漏一处就是一次静默的「退避不递增」——曲线永远
    停在 15 分钟。

    有 `server_default`：非空列加到既有表上，老行没有别的办法填。取 0 而不是 1，
    语义是「当前这一串还没开始」，与全新建出来的行完全一致。
    """
    command.upgrade(_config(database_url), "head")

    rounds = _columns(database_url, TASKS)[ROUNDS]
    assert rounds["nullable"] is False
    assert rounds["default"] is not None, "非空列没有默认值，老行填不进去"
    assert "0" in str(rounds["default"])


def test_an_existing_row_survives_the_upgrade_without_being_claimed(database_url: str) -> None:
    """升级**一行历史数据都不许改**：`MANUAL` 停着的任务升完还是要人工。

    ⚠️ 与 `c8d2a5f10b74`（`disabled_recovery`）**刻意不同**，那一条顺手认领了升级
    前就挂着「空闲航线不足」的行。这里不认领，因为库里已经分不清一次 `MANUAL`
    停用当初是「连崩到上限」还是「参数填错」——两者当时写的都是 `MANUAL`。
    认错的代价是把一条配置填错的链路每小时白起一次、永远不停。

    更要紧的是那个**用户手动关掉**的现场（`enabled=0` 且 `disabled_reason` 为
    NULL）：升级之后它必须原样关着，两列都是「没有在等冷却」。
    """
    config = _config(database_url)
    command.upgrade(config, DOWN_REVISION)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mission_tasks "
                "(kind, name, enabled, priority, params_json, consecutive_failures, "
                " disabled_reason, disabled_recovery, created_at_utc, updated_at_utc) "
                "VALUES "
                "('PIRATE', '侦查+攻击海盗', :off, 20, '{}', 0, NULL, NULL, "
                " :moment, :moment), "
                "('BOT', '5 系攻击', :on, 10, '{}', 3, "
                " '连续 3 次异常退出（退出码 1）', 'MANUAL', :moment, :moment)"
            ),
            {"off": False, "on": True, "moment": "2026-08-28 00:01:00"},
        )

    command.upgrade(config, "head")

    with engine.begin() as connection:
        rows = {
            name: (enabled, reason, recovery, retry_after, rounds)
            for name, enabled, reason, recovery, retry_after, rounds in connection.execute(
                text(
                    "SELECT name, enabled, disabled_reason, disabled_recovery, "
                    f"{RETRY_AFTER}, {ROUNDS} FROM mission_tasks"
                )
            ).all()
        }
    # 用户手动关掉的那一行：原样关着，而且没有任何闹钟。
    assert not rows["侦查+攻击海盗"][0]
    assert rows["侦查+攻击海盗"][1:] == (None, None, None, 0)
    # `MANUAL` 停着的那一行：标记一个字都没变，也没被安上重试时刻。
    assert rows["5 系攻击"][2] == "MANUAL"
    assert rows["5 系攻击"][3] is None
    assert rows["5 系攻击"][4] == 0


def test_downgrade_removes_only_the_new_columns(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    tasks = _columns(database_url, TASKS)
    assert RETRY_AFTER not in tasks
    assert ROUNDS not in tasks
    # 前几条迁移的成果还在：这条不该把别人的列一起带走。SQLite 上 `drop_column`
    # 走的是「重建整张表」，把别人的列漏掉一个不会有任何报错。
    assert "disabled_recovery" in tasks
    assert "enabled_from_utc" in tasks
    assert "quota_exhausted_until_utc" in tasks


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    assert RETRY_AFTER in _columns(database_url, TASKS)
    assert ROUNDS in _columns(database_url, TASKS)
