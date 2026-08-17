"""「人工释放航线」那一列的迁移必须能升能降，且两种方言都建对。

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸。这里两边对着比一遍。

⚠️ **方言取决于跑在哪**：设了 `EVO_HELPER_TEST_DATABASE_URL`（CI 上就是）时这几条
跑在真 Postgres 上，不设时仍是 SQLite。最后一条不建库、直接对迁移里那个分岔函数
断言，所以两种方言下它都在守着——SQLite 那一轮里它是唯一守得住 Postgres 那一半的。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command
from support.database import scratch_database_url

REVISION = "a9d5f31c0e77"
DOWN_REVISION = "a7f2c9d40b16"
COLUMN = "line_released_at_utc"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "migration.db")


def test_upgrade_adds_a_nullable_column(database_url: str) -> None:
    """必须可空：存量行的含义是「没人手动放过手」，那就是 NULL。

    给它一个非空默认值会把整库的历史派遣一次性标成「人工已释放」——
    也就是把在飞的舰队全部当成已回港。
    """
    command.upgrade(_config(database_url), "head")

    columns = {
        column["name"]: column
        for column in inspect(create_engine(database_url)).get_columns("attack_dispatches")
    }
    assert COLUMN in columns
    assert columns[COLUMN]["nullable"] is True
    # 那两个钟一列都没动：它们记的是观测，不该被这条迁移碰。
    assert "line_free_at_utc" in columns
    assert "expected_report_at_utc" in columns


def test_downgrade_removes_the_column(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    columns = {
        column["name"]
        for column in inspect(create_engine(database_url)).get_columns("attack_dispatches")
    }
    assert COLUMN not in columns
    assert "line_free_at_utc" in columns


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    columns = {
        column["name"]
        for column in inspect(create_engine(database_url)).get_columns("attack_dispatches")
    }
    assert COLUMN in columns


def test_the_migration_matches_the_orm_model(database_url: str, tmp_path: Path) -> None:
    """迁移建出来的列，和 `create_all` 建出来的必须一模一样。

    分叉了不会有人报错——测试库走 `create_all`，真库走迁移，两边各自都对。
    """
    from evo_helper.storage.database import Base

    command.upgrade(_config(database_url), "head")
    migrated = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(create_engine(database_url)).get_columns("attack_dispatches")
    }

    # 另开一个库：迁移建的那份和 `create_all` 建的那份必须互不干扰，
    # 不然后建的那次 `checkfirst` 会直接跳过，比出来永远相等。
    orm_engine = create_engine(scratch_database_url(tmp_path, "orm.db"))
    Base.metadata.create_all(orm_engine)
    created = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(orm_engine).get_columns("attack_dispatches")
    }

    assert migrated == created


def test_the_column_is_timestamptz_on_postgres_and_plain_on_sqlite() -> None:
    """**生产那一半只有这条守得住。**

    Postgres 上建成不带时区的话，`storage.database.UTCDateTime` 写进去的 aware
    值会被静默截成 naive，读回来参与比较时不报错、只是安静地错——而这条链路上
    的一切（配额按 UTC 日切、航线钟比大小）都建立在「读出来是 aware 的 UTC」上。

    SQLite 那一侧同样要钉住：两种写法在 SQLite 方言上渲染出的都是 `DATETIME`，
    所以这里断言的是**分岔本身还在**，不是渲染结果有差别。
    """
    migration = _load_migration(f"{REVISION}_manual_line_release")

    assert migration._timestamp("postgresql").timezone is True
    assert migration._timestamp("sqlite").timezone is False


def _load_migration(name: str) -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
