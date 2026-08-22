from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from alembic import command
from evo_helper.config import Settings
from evo_helper.infrastructure.code_version import CodeVersion
from evo_helper.web.runtime import (
    _alembic_revision,
    _upgrade_database,
    create_runtime_app,
    record_startup_version,
)
from support.database import scratch_database_url


def _rewind_the_version_pointer(database_url: str) -> None:
    """只把 `alembic_version` 那一行退回上一条，**表结构一个字不动**。

    刻意不做成 `_upgrade_database` 的参数：那个函数是部署路径上的东西，
    为了测试给它加一个「降级」入口，等于在生产代码里留一把只有测试用的刀。

    ⚠️ **这里原先跑的是真的 `downgrade -1`，2026-08-18 换成了 `stamp`。**
    两条理由：

    - **它更像事故当时的样子。** 用例自己的 docstring 写着「事故当时那个库
      **已经建好了全部表**、只是版本落后几条」——那正是 `stamp` 造出来的局面，
      而 `downgrade` 会真的把最新那条迁移的列删掉，造出一个事故里没有的局面。
    - **`downgrade` 让这条用例被最新那条迁移绑架。** 只要最新迁移往
      `create_runtime_app` 启动时要读的表（`bot_targets`、`military_attack_config`）
      加一列，退回去之后 ORM 的 SELECT 就找不到那一列，用例红在
      `no such column` 上——而那和「构造 app 有没有推进版本」毫无关系。
      2026-08-18 `b7e4d0c93a15` 往 `bot_targets` 加列时正是这么红的。
    """
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    script = ScriptDirectory.from_config(config)
    head = script.get_current_head()
    assert head is not None
    previous = script.get_revision(head).down_revision
    assert isinstance(previous, str), "迁移链只有一条，head 一定有唯一的上一条"
    command.stamp(config, previous)


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
    _rewind_the_version_pointer(database_url)
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


# -- 启动时把「代码版本 + 迁移前后的 revision」记进 system_log --------------------
#
# ⚠️ 这一节补的缺口：实机跑在另一台机器上，而库里原本查不出「代码停在哪个
# commit」。`alembic_version` 只说库升到了哪，`system_log` 的 host / pid 只说进程
# 什么时候换的——两样推不出「那台机器 pull 了没有」。没 pull 的机器重启 bat，
# 只会把库升到**旧 commit 所知的 head**，而我们会误以为已经升到 `main` 的 head。


class _Recorded:
    """把 `record_system_log` 的调用记下来。签名与真的那一个一致。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, level, source, message, *, payload=None, **_):  # type: ignore[no-untyped-def]
        self.calls.append((level, message, dict(payload or {})))


def test_the_startup_line_records_the_revision_before_and_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ **两个 revision 都要记，只记一个就答不出「这次重启到底升没升」。**

    这里造的正是实机上那个形状：库已经升到 head 了，重启 bat 什么都没动。
    """
    recorded = _Recorded()
    monkeypatch.setattr("evo_helper.web.runtime.record_system_log", recorded, raising=True)
    database_url = scratch_database_url(tmp_path, "startup-log.db")
    _upgrade_database(database_url)
    head = _current_revision(database_url)

    record_startup_version(revision_before=head, revision_after=head)

    level, message, payload = recorded.calls[0]
    assert (payload["revision_before"], payload["revision_after"]) == (head, head)
    assert payload["upgraded"] is False
    assert "库没动" in message
    assert level == "INFO"


def test_a_real_upgrade_says_it_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """升了就要看得出来：两个 revision 不一样，正文也说「升到」。

    ⚠️ 把两列合成一列（只记 `revision_after`）的话，这一条和上一条会变成同一句话，
    而「升没升」这个问题——现在最查不出来的那个——就又没人回答了。
    """
    recorded = _Recorded()
    monkeypatch.setattr("evo_helper.web.runtime.record_system_log", recorded, raising=True)

    record_startup_version(revision_before=None, revision_after="a3c81f5d2b64")

    _, message, payload = recorded.calls[0]
    assert payload["upgraded"] is True
    assert payload["revision_before"] is None
    assert "升到 a3c81f5d2b64" in message


def test_the_startup_line_carries_the_code_version_including_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **commit / 分支 / dirty 三样都要进 payload，`dirty` 尤其不许省。**

    有未提交改动正是「跑的代码和 `main` 不一样」的最强信号。
    """
    recorded = _Recorded()
    monkeypatch.setattr("evo_helper.web.runtime.record_system_log", recorded, raising=True)
    monkeypatch.setattr(
        "evo_helper.web.runtime.read_code_version",
        lambda: CodeVersion(commit="5447ca5", branch="main", dirty=True),
    )

    record_startup_version(revision_before="x", revision_after="x")

    _, message, payload = recorded.calls[0]
    assert (payload["commit"], payload["branch"], payload["dirty"]) == ("5447ca5", "main", True)
    assert "有未提交改动" in message


def test_nothing_local_beyond_the_three_fields_leaks_into_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 仓库是公开的，日志会被贴出来看。

    本地路径、用户名、远端地址一概不许进 payload——三样加两个 revision 就够了。
    """
    recorded = _Recorded()
    monkeypatch.setattr("evo_helper.web.runtime.record_system_log", recorded, raising=True)

    record_startup_version(revision_before="x", revision_after="y")

    _, _, payload = recorded.calls[0]
    assert set(payload) == {
        "commit",
        "branch",
        "dirty",
        "revision_before",
        "revision_after",
        "upgraded",
    }


def test_a_startup_line_never_takes_the_service_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **最严重的回归：一个纯观测的功能让控制台起不来。**

    git 不在 PATH / 不是 git 仓库时 `read_code_version` 自己降级（那边有用例），
    这一条守的是**上层**：拿到一份「三样全不知道」的版本，照样记得出一行，
    不抛。
    """
    recorded = _Recorded()
    monkeypatch.setattr("evo_helper.web.runtime.record_system_log", recorded, raising=True)
    monkeypatch.setattr(
        "evo_helper.web.runtime.read_code_version",
        lambda: CodeVersion(commit=None, branch=None, dirty=None),
    )

    record_startup_version(revision_before=None, revision_after=None)

    _, message, payload = recorded.calls[0]
    assert "取不到" in message
    assert payload["dirty"] is None, "取不到不许写成 False"


def test_a_fresh_database_has_no_revision_yet(tmp_path: Path) -> None:
    """全新库上 `alembic_version` 还不存在——那正是「升级前没有版本」这个事实。

    读不到不许抛：第一次启动走的就是这条路。
    """
    database_url = scratch_database_url(tmp_path, "fresh.db")

    assert _alembic_revision(database_url) is None

    _upgrade_database(database_url)

    assert _alembic_revision(database_url) == _current_revision(database_url)


def _current_revision(database_url: str) -> str:
    with create_engine(database_url).connect() as connection:
        value = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert value is not None
    return str(value)
