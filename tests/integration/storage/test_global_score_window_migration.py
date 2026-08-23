"""选靶窗口（有效期 + 窗口门限）搬进全局攻击配置那一条迁移，以及它当前就是 head。

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸。这里两边对着比一遍。

⚠️ **方言取决于跑在哪**：设了 `EVO_HELPER_TEST_DATABASE_URL`（CI 上就是）时这几条
跑在真 Postgres 上，不设时仍是 SQLite。

⚠️ **这条迁移一次都没有在任何真实库上执行过。** 生产自己在启动时升，开发一侧
不碰（CLAUDE.md 的硬约束）；这份用例跑的全是临时库。

这一条的关键性质有三条，每条下面都有一条用例守着：

1. **两列都可空、都没有 `server_default`**——NULL = 「跟着代码里的默认值走」
   （2 小时 / 100 个），这正是升级完成那一刻行为完全不变的保证。
2. **有效期是浮点**——1.5 小时一直是合法取值，存成整数会把它悄悄取整。
3. **一个业务数据都不动**——存量任务 `params_json` 里那两个旧键照原样留着，
   既不删也不往新列里搬（理由整段在迁移的模块头上：搬哪一份都是替用户拍一个数）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from alembic import command
from support.database import scratch_database_url

REVISION = "c7f2a91d4e08"
DOWN_REVISION = "b8e1c4a72f05"
CONFIG = "military_attack_config"
MAX_AGE = "score_max_age_hours"
FLOOR = "window_floor"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "global-score-window-migration.db")


def _columns(database_url: str, table: str) -> dict[str, dict[str, object]]:
    return {
        column["name"]: column for column in inspect(create_engine(database_url)).get_columns(table)
    }


def test_this_revision_is_the_only_head() -> None:
    """链上只有一个 head，而且就是这一条。

    生产靠启动时 `alembic upgrade head` 自升（`web.runtime._upgrade_database`），
    多一个 head 就是用户重启 bat 之后控制台直接起不来——而这件事在合并之前一个字
    都看不出来。

    ⚠️ 「head 就是我」这句话只该由**最新那一条**迁移的用例来说。再接新迁移时，
    把这一条降级成「我在链上」（照
    `test_bot_target_unreadable_migration.py` 里那一段），别两处都断言 head。
    """
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert list(script.get_heads()) == [REVISION]


def test_both_columns_are_nullable_with_no_default(database_url: str) -> None:
    """**两列都可空，而且一个默认值都不许给。**

    NULL 的含义是「跟着代码里的默认值走」（`DEFAULT_SCORE_MAX_AGE` = 2 小时、
    `WINDOW_POOL_FLOOR` = 100）。给了默认值，「没配」和「恰好配成了当前默认」
    就分不开了——日后把代码里那两个数调掉时，所有老行都被钉死在旧数上，
    而它们表达的其实是「跟着默认走」。
    """
    command.upgrade(_config(database_url), "head")

    config = _columns(database_url, CONFIG)
    for column in (MAX_AGE, FLOOR):
        assert column in config
        assert config[column]["nullable"] is True
        assert config[column]["default"] is None, "有了默认值，日后调代码里的默认值它不跟"


def test_the_max_age_column_takes_a_fraction(database_url: str) -> None:
    """有效期那一列必须存得住 **1.5 小时**。

    守的是一个具体的静默失真：页面上这一格的步长一直是 0.5，用户填 1.5 是合法的。
    这一列若是 `Integer`，1.5 会被库悄悄变成 1——窗口窄了三分之一，而日志里写着
    1.0，看起来完全正常。这条用例存在的意义就是让「换成 Integer」当场转红。
    """
    command.upgrade(_config(database_url), "head")

    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text(f"UPDATE {CONFIG} SET {MAX_AGE} = 1.5 WHERE id = 1"))
    with engine.connect() as connection:
        stored = connection.execute(text(f"SELECT {MAX_AGE} FROM {CONFIG}")).scalar()
    assert stored == pytest.approx(1.5)


def test_the_existing_row_comes_out_null(database_url: str) -> None:
    """存量那一行升完必须是 NULL——升级完成那一刻行为完全不变的保证。

    `military_attack_config` 只有 id=1 那一行（`f6c3d2a1b4e8` 建表时就插了它），
    也就是用户攻击配置页上的那一份。它升完若带上任何具体取值，等于替用户做了一次
    他没做过的配置——而这两格恰恰是「所有星系一起用哪个窗口」，拍错了全库都跟着变。
    """
    config = _config(database_url)
    command.upgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        stored = connection.execute(text(f"SELECT {MAX_AGE}, {FLOOR} FROM {CONFIG}")).all()
    assert stored, "存量行本身没了，这条用例就什么都没验到"
    assert all(row == (None, None) for row in stored)


def test_the_old_task_parameters_are_left_exactly_as_they_were(database_url: str) -> None:
    """存量任务 `params_json` 里那两个旧键**一个字都不动**。

    守的是「不替用户拍一个数」这条决定（理由整段在迁移的模块头上）：库里有多个军力
    任务，各存着自己的 `score_max_age_hours` / `top_n`，往全局搬哪一份都是替用户
    选一个——而拍错的症状是**所有星系一起换了个有效期**，页面上却看不出这个数是
    从哪来的。

    ⚠️ **也不许顺手把旧键删掉。** 删了就没法回滚，而这条迁移在真实库上跑之前无从
    验证。代码侧的善后是「读到就忽略并打 WARNING」
    （`application.mission_scheduler._legacy_window_keys`），任务页保存一次就清掉。
    """
    config = _config(database_url)
    command.upgrade(config, DOWN_REVISION)
    engine = create_engine(database_url)
    params = json.dumps({"by_military": True, "top_n": 7, "score_max_age_hours": 6.0})
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mission_tasks "
                "(kind, enabled, priority, params_json, consecutive_failures, "
                "created_at_utc, updated_at_utc) "
                "VALUES ('BOT', :enabled, 1, :params, 0, "
                "'2026-08-23 00:00:00', '2026-08-23 00:00:00')"
            ),
            # ⚠️ **`enabled` 要绑成 Python 的 `True`，不能在 SQL 里写字面量 `1`。**
            # 本地默认跑 SQLite（1 就是真），而 CI 跑 PostgreSQL——那边这一列是
            # `boolean`，塞整数直接 `DatatypeMismatch`：
            #   column "enabled" is of type boolean but expression is of type integer
            # 绑成参数之后由方言各自适配，两边都过。这条本地绿、CI 红过一次。
            {"params": params, "enabled": True},
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        stored = connection.execute(text("SELECT params_json FROM mission_tasks")).scalars().all()
    assert [json.loads(raw) for raw in stored] == [
        {"by_military": True, "top_n": 7, "score_max_age_hours": 6.0}
    ]


def test_downgrade_removes_only_the_new_columns(database_url: str) -> None:
    """退回去只带走这两列，别人的旋钮一个都不许被顺手删掉。

    这张表上十来个旋钮是好几个 PR 各加各的，`batch_alter_table` 在 SQLite 上是
    「建新表 + 搬数据」——一次写错就是整张表的其它列陪着消失，而症状要等下一次
    读配置时才露头。
    """
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    surviving = _columns(database_url, CONFIG)
    assert MAX_AGE not in surviving
    assert FLOOR not in surviving
    assert "tiers_json" in surviving
    assert "blind_scroll_rows" in surviving
    assert "unreadable_exclusion_hours" in surviving
    assert "auto_toggle_log_seconds" in surviving
