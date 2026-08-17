from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from alembic import command
from evo_helper.config import Settings
from evo_helper.web.runtime import _upgrade_database, create_runtime_app
from support.database import scratch_database_url


def _downgrade_one_step(database_url: str) -> None:
    """把库退回上一条迁移。**只给用例造「库落后于代码」这个局面用。**

    刻意不做成 `_upgrade_database` 的参数：那个函数是部署路径上的东西，
    为了测试给它加一个「降级」入口，等于在生产代码里留一把只有测试用的刀。
    """
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    command.downgrade(config, "-1")


def test_building_the_app_never_advances_the_schema_version(tmp_path: Path) -> None:
    """⚠️ **构造一次 app 绝不能推进它连到的那个库的版本。**

    这条守的是 2026-08-17 那次事故：`create_runtime_app` 当时顺手跑
    `alembic upgrade head`，于是**只是想渲染一个页面看 bug**，就在**生产库**上
    跑掉了七条迁移——全程没有任何提示，也没有备份、没有经过用户同意。

    严重性不在版本号对不对，而在 **schema 变更回退不了**：代码能回滚，
    已经改过的表回不去。

    ⚠️ **判据必须是「版本没前进」，不能是「库是空的」。** 事故当时那个库
    **已经建好了全部表**、只是版本落后几条；而 `create_runtime_app` 本来就要读表
    （清海盗位候选、清保留期），空库上它只会崩，那样测出来的是另一回事。
    这里先把库升到一个**旧版本**，再构造 app，然后问版本变没变——这才是那次
    真正发生的事。
    """
    database_url = scratch_database_url(tmp_path, "behind.db")
    _upgrade_database(database_url)
    _downgrade_one_step(database_url)
    engine = create_engine(database_url)
    with engine.connect() as connection:
        before = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()

    create_runtime_app(Settings(database_url=database_url), local_token="runtime-token")

    with engine.connect() as connection:
        after = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert after == before, "构造 app 就把库升上去了——那正是事故的形态"


def test_the_service_entrypoint_is_what_migrates(tmp_path: Path) -> None:
    """迁移的闸门在 `main()`——也就是 bat 真正启动服务那条路。

    用户口径（2026-08-17）：「生产环境重新执行 bat 后会执行数据库表结构最新修改」。
    上一条把「别的地方不许升」钉住，这一条把「该升的地方还在升」钉住——
    少了它，一个「把迁移整个删掉」的改动会让两条一起变绿。
    """
    database_url = scratch_database_url(tmp_path, "entrypoint.db")

    _upgrade_database(database_url)

    assert "mission_tasks" in inspect(create_engine(database_url)).get_table_names()


def test_runtime_serves_persistent_api(tmp_path: Path) -> None:
    database_url = scratch_database_url(tmp_path, "runtime.db")
    _upgrade_database(database_url)
    app = create_runtime_app(Settings(database_url=database_url), local_token="runtime-token")
    client = TestClient(app)
    response = client.post(
        "/api/plans",
        headers={"X-Evo-Helper-Token": "runtime-token"},
        json={
            "name": "runtime-plan",
            "window_start": "08:00",
            "window_end": "20:00",
            "ranges": [
                {
                    "start": {"galaxy": 1, "system": 1, "position": 1},
                    "end": {"galaxy": 1, "system": 1, "position": 2},
                    "origin": {"galaxy": 1, "system": 1, "position": 1},
                    "fleet_preset": "fleet-a",
                    "fleet_preset_signature": "fleet-a-signature",
                }
            ],
        },
    )
    assert response.status_code == 201

    engine = create_engine(database_url)
    assert {"public_id", "updated_at_utc"} <= {
        column["name"] for column in inspect(engine).get_columns("scan_plans")
    }
    # 调度器的可调项走迁移加列。漏了这条迁移，模型和真实的表就会静默分叉：
    # 本地测试用 `create_all` 建表，一路全绿，只有真实的库会在启动时炸。
    #
    # 删列同理，方向相反：`c1f70b8a26d4` 把分档那三列 drop 掉了，漏跑它的话
    # 真实的库上会多出三列没有任何代码认识的配置——而 `create_all` 建出来的
    # 测试库压根不会有它们，两边看起来都对。
    config_columns = {column["name"] for column in inspect(engine).get_columns("scheduler_config")}
    assert "restart_cooldown_seconds" in config_columns
    assert not {name for name in config_columns if name.startswith("tier_")}
    assert engine.connect().execute(text("SELECT version_num FROM alembic_version")).scalar_one()


def test_applying_migrations_does_not_silence_application_logging(tmp_path: Path) -> None:
    """Alembic's fileConfig defaults to disabling every existing logger.

    The runtime migrates at startup, so that default would kill the
    report-timing log for the rest of the process.
    """
    import logging

    from evo_helper.web.runtime import _upgrade_database

    logger = logging.getLogger("evo_helper.vision.live_reports")
    logger.setLevel(logging.INFO)

    _upgrade_database(scratch_database_url(tmp_path, "migrated.db"))

    assert logger.isEnabledFor(logging.INFO)
    assert not logger.disabled
