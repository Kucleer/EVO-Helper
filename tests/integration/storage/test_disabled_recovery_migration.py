"""「停用类别」那一列的迁移必须能升能降，且两种方言都跑得过。

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸。这里两边对着比一遍。

⚠️ **生产是 Postgres，测试跑 SQLite。** 这一条迁移刻意只用两种方言都直接支持的
东西——可空 `VARCHAR` 的 `ADD COLUMN`、一条带绑定参数的 `UPDATE ... LIKE`——
所以它整个没有按方言分岔的分支。最后一条用例把这一点钉住：任何一天有人往里
加了方言相关的写法（`ALTER COLUMN`、`server_default`、`batch_alter_table`），
这条就该被重新想一遍。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

REVISION = "c8d2a5f10b74"
DOWN_REVISION = "b3f5c8d10a27"
COLUMN = "disabled_recovery"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'migration.db'}"


def _columns(database_url: str) -> dict[str, dict[str, object]]:
    return {
        column["name"]: column
        for column in inspect(create_engine(database_url)).get_columns("mission_tasks")
    }


def test_upgrade_adds_a_nullable_column(database_url: str) -> None:
    """必须可空：本列上线之前每一行的语义都是「只有人能放它出来」，那就是 NULL。

    给它一个非空默认值，等于要多解释一次「没停用的行为什么也带着一个类别」，
    而 NULL 一律当 `MANUAL` 读本来就是唯一安全的默认。
    """
    command.upgrade(_config(database_url), "head")

    columns = _columns(database_url)
    assert COLUMN in columns
    assert columns[COLUMN]["nullable"] is True
    # 停用原因那一列一个字都没动：它仍然是写给人看的那句话。
    assert "disabled_reason" in columns


def test_downgrade_removes_the_column(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    columns = _columns(database_url)
    assert COLUMN not in columns
    assert "disabled_reason" in columns


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """升 → 降 → 再升。不可重放的迁移等于把「退回来再试」这条路堵死。"""
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)

    command.upgrade(config, "head")

    assert COLUMN in _columns(database_url)


def test_a_task_already_disabled_for_lack_of_lines_is_claimed_on_upgrade(
    database_url: str,
) -> None:
    """升级之前就挂着「空闲航线不足」的那一行，升完直接带上会自愈的标记。

    不认领的话，2026-08-17 生产库里那条已经停用的 bot 任务升级完照样要用户
    手点一次「恢复」——而它正是这次改动要修的那一条。

    ⚠️ 认领这一下**是唯一看中文的地方，而且只看历史数据**。运行期的判据一个字
    都不比对文案，那正是加这一列的理由。
    """
    config = _config(database_url)
    command.upgrade(config, DOWN_REVISION)
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO mission_tasks "
                "(kind, name, enabled, priority, params_json, consecutive_failures, "
                " disabled_reason, created_at_utc, updated_at_utc) "
                "VALUES "
                "('BOT', '扫描+攻击 bot', 1, 10, '{}', 0, "
                " '空闲航线不足，暂不启动 bot 攻击', "
                " '2026-08-17 00:00:00', '2026-08-17 00:00:00'), "
                "('PIRATE', '海盗', 1, 20, '{}', 3, "
                " '连续 3 次异常退出（退出码 1）', "
                " '2026-08-17 00:00:00', '2026-08-17 00:00:00'), "
                "('SCAN', '扫描', 1, 30, '{}', 0, NULL, "
                " '2026-08-17 00:00:00', '2026-08-17 00:00:00')"
            )
        )

    command.upgrade(config, "head")

    with engine.begin() as connection:
        claimed = dict(
            connection.execute(text("SELECT kind, disabled_recovery FROM mission_tasks")).all()
        )
    assert claimed["BOT"] == "FREE_LINES"
    # 连续失败那一行绝不能被顺手认领：它说的是「这不是暂时的」。
    assert claimed["PIRATE"] is None
    assert claimed["SCAN"] is None


def test_the_migration_matches_the_orm_model(database_url: str) -> None:
    """迁移建出来的列，和 `create_all` 建出来的必须一模一样。

    分叉了不会有人报错——测试库走 `create_all`，真库走迁移，两边各自都对。
    """
    from evo_helper.storage.database import Base

    command.upgrade(_config(database_url), "head")
    migrated = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(create_engine(database_url)).get_columns("mission_tasks")
    }

    orm_url = database_url.replace("migration.db", "orm.db")
    orm_engine = create_engine(orm_url)
    Base.metadata.create_all(orm_engine)
    created = {
        column["name"]: (str(column["type"]), column["nullable"])
        for column in inspect(orm_engine).get_columns("mission_tasks")
    }

    assert migrated == created


def test_the_migration_has_no_dialect_specific_branch() -> None:
    """这条迁移**刻意不按方言分岔**，因为它用的东西两种方言都直接支持。

    钉住它，是为了让将来任何一次「加个 `server_default`」「顺手 `ALTER COLUMN`」
    当场转红——那些在 SQLite 上要走 `batch_alter_table` 重建整张表，而重建会
    连带丢掉本迁移看不见的东西。转红时该做的是补上分岔（写法见
    `a9d5f31c0e77_manual_line_release.py`），不是删掉这条用例。
    """
    root = Path(__file__).resolve().parents[3]
    source = (root / "alembic" / "versions" / f"{REVISION}_disabled_recovery.py").read_text(
        encoding="utf-8"
    )
    # 认**代码里的名字**，不是整份文件的文本：注释里正解释着为什么不用这几样，
    # 裸的子串匹配会被自己的注释绊倒。
    tree = ast.parse(source)
    used = {
        node.attr if isinstance(node, ast.Attribute) else node.arg
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute | ast.keyword)
    } | {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert "get_bind" not in used
    assert "batch_alter_table" not in used
    assert "server_default" not in used
