"""`blind_scroll_rows`（盲滚多少**行**）那一列的迁移，以及它当前就是 head。

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸。这里两边对着比一遍。

⚠️ **方言取决于跑在哪**：设了 `EVO_HELPER_TEST_DATABASE_URL`（CI 上就是）时这几条
跑在真 Postgres 上，不设时仍是 SQLite。

⚠️ **这条迁移一次都没有在任何真实库上执行过。** 生产自己在启动时升，开发一侧
不碰（CLAUDE.md 的硬约束）；这份用例跑的全是临时库。

这一列的关键性质有两条，两条都在下面各有一条用例守着：

1. **可空、无 `server_default`**——NULL = 「跟着代码里的默认值
   `game.ranking_ui.BLIND_SCROLL_ROWS`(700) 走」。
2. **`blind_scrolls`（屏）没被这条迁移带走**——它是这次改动的一键回滚。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from alembic import command
from support.database import scratch_database_url

REVISION = "b8e1c4a72f05"
DOWN_REVISION = "a3c81f5d2b64"
CONFIG = "military_attack_config"
ROWS = "blind_scroll_rows"
SCROLLS = "blind_scrolls"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "blind-scroll-rows-migration.db")


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


def test_the_column_is_nullable_with_no_default(database_url: str) -> None:
    """**可空，而且一个默认值都不许给。**

    NULL 的含义是「跟着代码里的默认值 `BLIND_SCROLL_ROWS`(700) 走」。给了默认值，
    「没配」和「恰好配成了当前默认」就分不开了——日后把代码里的 700 调成别的数时，
    所有老行都被钉死在 700 上，而它们表达的其实是「跟着默认走」。
    """
    command.upgrade(_config(database_url), "head")

    config = _columns(database_url, CONFIG)
    assert ROWS in config
    assert config[ROWS]["nullable"] is True
    assert config[ROWS]["default"] is None, "有了默认值，日后调代码里的默认值它不跟"


def test_the_existing_row_comes_out_null(database_url: str) -> None:
    """存量那一行升完必须是 NULL——升级完成那一刻行为完全不变的保证。

    `military_attack_config` 只有 id=1 那一行（`f6c3d2a1b4e8` 建表时就插了它），
    也就是用户攻击配置页上的那一份。它升完若带上任何具体行数，等于替用户做了一次
    他没做过的配置。
    """
    config = _config(database_url)
    command.upgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    with engine.connect() as connection:
        stored = connection.execute(text(f"SELECT {ROWS} FROM {CONFIG}")).scalars().all()
    assert stored, "存量行本身没了，这条用例就什么都没验到"
    assert all(value is None for value in stored)


def test_the_screens_column_survives_as_the_rollback_lever(database_url: str) -> None:
    """`blind_scrolls`（屏）**刻意保留不删**，这条用例就是那句话的钉子。

    它是这次改动的一键回滚：`blind_scroll_rows` 置空即退回慢拖那条路，不需要改
    代码、不需要再来一条迁移。顺手把它删掉，回滚就变成「改代码 + 重新发版」。
    """
    command.upgrade(_config(database_url), "head")

    assert SCROLLS in _columns(database_url, CONFIG)


def test_downgrade_removes_only_the_new_column(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    assert ROWS not in _columns(database_url, CONFIG)
    # 别人的列不许被一起带走：屏那一列、以及上一批旋钮里的两条。
    surviving = _columns(database_url, CONFIG)
    assert SCROLLS in surviving
    assert "unreadable_exclusion_hours" in surviving


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    assert ROWS in _columns(database_url, CONFIG)


def test_the_migration_matches_the_orm_model(database_url: str, tmp_path: Path) -> None:
    """迁移建出来的列，和 `create_all` 建出来的必须一模一样。

    分叉了不会有人报错——测试库走 `create_all`，真库走迁移，两边各自都对，
    只有生产会在启动升级后拿一张缺列的表跑起来。
    """
    from evo_helper.storage.database import Base

    command.upgrade(_config(database_url), "head")
    migrated = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(create_engine(database_url)).get_columns(CONFIG)
    }

    # 另开一个库：迁移建的那份和 `create_all` 建的那份必须互不干扰，
    # 不然后建的那次 `checkfirst` 会直接跳过，比出来永远相等。
    orm_engine = create_engine(scratch_database_url(tmp_path, "orm-blind-scroll-rows.db"))
    Base.metadata.create_all(orm_engine)
    created = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(orm_engine).get_columns(CONFIG)
    }

    assert migrated == created
