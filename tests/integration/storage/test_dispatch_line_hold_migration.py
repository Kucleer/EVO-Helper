"""`attack_dispatches.line_hold_until_utc`：可空、无默认值，而且它就是当前的 head。

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

REVISION = "d1a7f4b26c93"
DOWN_REVISION = "c4f8a2e51b07"
TABLE = "attack_dispatches"
COLUMN = "line_hold_until_utc"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "line-hold-migration.db")


def _columns(database_url: str, table: str) -> dict[str, dict[str, object]]:
    return {
        column["name"]: column for column in inspect(create_engine(database_url)).get_columns(table)
    }


def test_this_revision_is_the_only_head() -> None:
    """整条迁移链只有一个 head，而且就是这一条（眼下最新的那一条）。

    生产靠启动时 `alembic upgrade head` 自升（`web.runtime._upgrade_database`），
    多一个 head 就是用户重启 bat 之后控制台直接起不来——而这件事在合并之前
    一个字都看不出来。合并时若发现别的分支也挂在 `c4f8a2e51b07` 上，
    **后进的那条改自己的 `down_revision`**。
    """
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert list(script.get_heads()) == [REVISION]


def test_the_column_is_nullable_with_no_default(database_url: str) -> None:
    """⚠️ **可空，而且一个默认值都不许给。**

    存量行没有这个估算。给个 `server_default` 就等于替历史行编一个看起来像
    算过的兜底时刻，而这一列的全部意义是「算得出来就用它，算不出来就退回那个
    常数」——NULL 才是「算不出来」的说法（判据在
    `storage.repository._still_holding_a_line`，NULL 参与比较得 NULL、也就是假）。
    """
    command.upgrade(_config(database_url), "head")

    columns = _columns(database_url, TABLE)
    assert COLUMN in columns
    assert columns[COLUMN]["nullable"] is True
    assert columns[COLUMN]["default"] is None, "有了默认值，存量行就会凭空多出一个兜底时刻"


def test_downgrade_removes_only_the_new_column(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    assert COLUMN not in _columns(database_url, TABLE)
    # 上一条迁移的成果还在：这条不该把别人的列一起带走。
    assert "flight_source" in _columns(database_url, TABLE)
    assert "line_free_at_utc" in _columns(database_url, TABLE)


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    assert COLUMN in _columns(database_url, TABLE)


def test_the_migration_matches_the_orm_model(database_url: str, tmp_path: Path) -> None:
    """迁移建出来的列，和 `create_all` 建出来的必须一模一样。

    分叉了不会有人报错——测试库走 `create_all`，真库走迁移，两边各自都对。
    """
    from evo_helper.storage.database import Base

    command.upgrade(_config(database_url), "head")
    migrated = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(create_engine(database_url)).get_columns(TABLE)
    }

    # 另开一个库：迁移建的那份和 `create_all` 建的那份必须互不干扰，
    # 不然后建的那次 `checkfirst` 会直接跳过，比出来永远相等。
    orm_engine = create_engine(scratch_database_url(tmp_path, "orm-line-hold.db"))
    Base.metadata.create_all(orm_engine)
    created = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(orm_engine).get_columns(TABLE)
    }

    assert migrated == created
