"""调度台的 API。

写请求只有同源校验（局域网内浏览器天然同源），所以这里只测行为，不测鉴权——
那是 `web/security.py` 的事。

**这里不真的 Popen 任何 runner**：`launch` 一律注入假的。真起一个会去点用户的
真实鼠标、派真实舰队。后台 tick 也被推到一小时一次，免得测试里冒出计划外的启动。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evo_helper.application.mission_freeze import DEFAULT_FREEZE_LOG, MissionFreezeLog
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.tools.scan_console import parse_scheduler
from evo_helper.web.app import create_persistent_app

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
TOKEN = "test-token"


class FakeProcess:
    """`subprocess.Popen` 里 supervisor 用到的那一小部分。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.exit_code = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code if self.exit_code is not None else 0


class FakeLauncher:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, kind: MissionKind, command: Sequence[str], log_path: Path) -> FakeProcess:
        self.commands.append(tuple(command))
        return FakeProcess(pid=9000 + len(self.commands))


class MovableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Console:
    client: TestClient
    repository: SqlAlchemyRepository
    scheduler: MissionScheduler
    launcher: FakeLauncher
    clock: MovableClock
    #: 配置固化记录落盘的地方。**临时目录**，测试不许往仓库里写文件。
    freeze_log: Path

    def get(self) -> dict[str, object]:
        response = self.client.get("/api/scheduler")
        assert response.status_code == 200, response.text
        body: dict[str, object] = response.json()
        return body

    def task(self, kind: str) -> dict[str, object]:
        tasks = self.get()["tasks"]
        assert isinstance(tasks, list)
        for item in tasks:
            if item["kind"] == kind:
                found: dict[str, object] = item
                return found
        raise AssertionError(f"调度台上没有 {kind} 这一行")


def _seed_bot(repository: SqlAlchemyRepository, coordinate: Coordinate) -> None:
    """往 `bot_targets` 里放一颗已记录的 bot。

    「范围内有没有 bot」是启用 bot 链路的硬前提，所以它必须来自真实的库，
    不能靠打桩——打桩的话，这条判据断了测试也不会红。
    """
    with repository._session_factory() as session:  # noqa: SLF001 - 测试直接落库
        session.add(
            orm.BotTargetRow(
                galaxy=coordinate.galaxy,
                system=coordinate.system,
                position=coordinate.position,
                is_bot=True,
                latest_owner_name="bot",
                last_scanned_at_utc=NOW,
            )
        )
        session.commit()


@pytest.fixture
def console(tmp_path: Path) -> Iterator[Console]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'console.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    repository = SqlAlchemyRepository(factory)
    clock = MovableClock(NOW)
    launcher = FakeLauncher()
    supervisor = MissionSupervisor(launch=launcher, clock=clock, log_dir=tmp_path / "logs")
    freeze_log = tmp_path / "freezes.jsonl"
    scheduler = MissionScheduler(
        repository, supervisor, clock=clock, freeze_log=MissionFreezeLog(freeze_log)
    )
    app = create_persistent_app(
        factory,
        local_token=TOKEN,
        mission_scheduler=scheduler,
        # 后台 tick 先 sleep 再 tick，推到一小时就等于「测试期间不会自己跑」。
        tick_interval_s=3600.0,
    )
    with TestClient(app, headers={"X-Evo-Helper-Token": TOKEN}) as client:
        yield Console(client, repository, scheduler, launcher, clock, freeze_log)


# -- 读 -------------------------------------------------------------------------


def test_the_scheduler_starts_stopped(console: Console) -> None:
    """控制台重启后一律停在「已停止」。重启多半意味着出了事。"""
    body = console.get()

    assert body["running"] is False
    assert body["current"] is None
    assert body["started_at_utc"] is None


def test_the_three_tasks_are_listed_in_priority_order(console: Console) -> None:
    body = console.get()
    tasks = body["tasks"]
    assert isinstance(tasks, list)

    assert sorted(item["kind"] for item in tasks) == ["BOT", "PIRATE", "SCAN"]
    priorities = [item["priority"] for item in tasks]
    assert priorities == sorted(priorities)


def test_every_task_carries_a_status_and_a_detail(console: Console) -> None:
    """悬浮窗是个瘦客户端：它不查库，状态那句话必须由这个接口给全。"""
    assert console.task("SCAN")["status"] == "待命"
    assert console.task("PIRATE")["status"] == "未启用"
    assert console.task("BOT")["status"] == "未启用"


def test_the_pirate_row_echoes_the_systems_its_radius_covers(console: Console) -> None:
    """半径 10 是多大范围，用户心里没数；回显出来才看得见填错没有。"""
    summary = console.task("PIRATE")["summary"]
    assert isinstance(summary, str)
    assert "2:127" in summary
    assert "2:147" in summary
    assert "21" in summary


def test_the_bot_row_echoes_how_many_bots_the_range_holds(console: Console) -> None:
    """N=0 就禁止启用，所以 N 必须先看得见。"""
    _seed_bot(console.repository, Coordinate(2, 150, 4))
    console.client.patch(
        "/api/missions/BOT",
        json={"params": {"galaxy": 2, "first_system": 100, "last_system": 200}},
    )

    summary = console.task("BOT")["summary"]
    assert isinstance(summary, str)
    assert "1" in summary


# -- 开始 / 结束 -----------------------------------------------------------------


def test_starting_and_stopping_flips_the_flag(console: Console) -> None:
    assert console.client.post("/api/scheduler/start").status_code == 200
    assert console.get()["running"] is True

    assert console.client.post("/api/scheduler/stop").status_code == 200
    assert console.get()["running"] is False


def test_the_running_child_is_reported_with_its_log(console: Console) -> None:
    """悬浮窗要显示「当前跑的是哪条链路、已运行多久」，两样都从这里取。"""
    console.client.post("/api/scheduler/start")
    console.scheduler.tick()
    console.clock.now = NOW + timedelta(minutes=2)

    body = console.get()
    current = body["current"]
    assert isinstance(current, dict)
    assert current["kind"] == "SCAN"
    assert current["started_at_utc"].startswith("2026-08-09T12:00")
    assert "mission-scan.log" in current["log_path"]
    assert console.task("SCAN")["status"] == "运行中"


def test_stopping_kills_the_child(console: Console) -> None:
    console.client.post("/api/scheduler/start")
    console.scheduler.tick()
    assert console.get()["current"] is not None

    console.client.post("/api/scheduler/stop")

    assert console.get()["current"] is None


# -- PATCH ----------------------------------------------------------------------


def test_priority_can_be_reordered(console: Console) -> None:
    assert console.client.patch("/api/missions/BOT", json={"priority": -1}).status_code == 200

    tasks = console.get()["tasks"]
    assert isinstance(tasks, list)
    assert tasks[0]["kind"] == "BOT"


def test_the_scan_priority_cannot_be_written(console: Console) -> None:
    """扫描恒在最后一位。

    领域层的排序键已经结构性地保证了这一点，所以接受一个 priority 写入不会
    真的改变次序——正因为如此才必须拒绝：默默收下一个不起作用的值，页面会
    显示成「排序已保存」，刷新后又弹回去，用户只能得出「这个控件坏了」。
    """
    response = console.client.patch("/api/missions/SCAN", json={"priority": 0})

    assert response.status_code == 400
    assert "扫描" in response.json()["detail"]
    tasks = console.get()["tasks"]
    assert isinstance(tasks, list)
    assert tasks[-1]["kind"] == "SCAN"


def test_the_scan_row_can_still_be_switched_off(console: Console) -> None:
    """挡的只是 priority 那一个字段，别把整行改成只读。"""
    response = console.client.patch("/api/missions/SCAN", json={"enabled": False})

    assert response.status_code == 200
    assert console.task("SCAN")["enabled"] is False


def test_a_bot_range_with_no_recorded_bots_is_refused(console: Console) -> None:
    """拉起一个必然空转的 runner 没有意义，早一步告诉用户。"""
    response = console.client.patch(
        "/api/missions/BOT",
        json={"enabled": True, "params": {"galaxy": 9, "first_system": 1, "last_system": 2}},
    )

    assert response.status_code == 400
    assert "没有已记录的 bot" in response.json()["detail"]
    assert console.task("BOT")["enabled"] is False


def test_enabling_a_bot_range_that_holds_bots_is_accepted(console: Console) -> None:
    _seed_bot(console.repository, Coordinate(2, 150, 4))

    response = console.client.patch(
        "/api/missions/BOT",
        json={"enabled": True, "params": {"galaxy": 2, "first_system": 100, "last_system": 200}},
    )

    assert response.status_code == 200
    assert console.task("BOT")["enabled"] is True


def test_enabling_without_params_still_checks_the_stored_ones(console: Console) -> None:
    """勾复选框那一下也要过校验——否则先存一个空范围、再单独勾上就绕过去了。"""
    response = console.client.patch("/api/missions/BOT", json={"enabled": True})

    assert response.status_code == 400
    assert console.task("BOT")["enabled"] is False


def test_a_non_positive_pirate_radius_is_refused(console: Console) -> None:
    response = console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 0}})

    assert response.status_code == 400


def test_a_reversed_system_range_is_refused(console: Console) -> None:
    response = console.client.patch(
        "/api/missions/BOT",
        json={"params": {"galaxy": 2, "first_system": 200, "last_system": 100}},
    )

    assert response.status_code == 400
    assert "颠倒" in response.json()["detail"]


def test_switching_a_task_off_never_needs_valid_params(console: Console) -> None:
    """关一条链路必须永远做得到。参数填错了还关不掉，那就真的没退路了。"""
    response = console.client.patch("/api/missions/BOT", json={"enabled": False})

    assert response.status_code == 200


def test_patching_clears_an_automatic_disable(console: Console) -> None:
    """参数填错一次、改好了也永远起不来，是最容易踩的那个坑。"""
    console.repository.disable_mission_task(MissionKind.PIRATE, "连续 3 次异常退出")

    console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 5}})

    assert console.task("PIRATE")["disabled_reason"] is None


def test_a_disabled_scan_is_revived_by_enabling_it_again(console: Console) -> None:
    """**页面上那个「恢复」按钮走的就是这一条。**

    扫描不吃参数、也不许改优先级，所以它只有 `enabled` 这一条改得动的路；
    而自动停用时 `enabled` 本来就还是 True——不认这一下的话，一条被
    「连续 3 次异常退出」停掉的扫描在页面上永远没有恢复的办法，用户只能去改库。
    计数也必须一起清零，否则下一次崩溃立刻又满三次。
    """
    console.repository.record_mission_failure(MissionKind.SCAN, exit_code=1, limit=1)
    assert console.task("SCAN")["disabled_reason"] is not None

    response = console.client.patch("/api/missions/SCAN", json={"enabled": True})

    assert response.status_code == 200, response.text
    assert console.task("SCAN")["disabled_reason"] is None
    assert console.task("SCAN")["status"] != "已停用"
    row = next(row for row in console.repository.mission_tasks() if row.kind == "SCAN")
    assert row.consecutive_failures == 0


def test_an_unknown_kind_is_a_404(console: Console) -> None:
    assert console.client.patch("/api/missions/DRAGON", json={"enabled": True}).status_code == 404


# -- 运行中不许改 ---------------------------------------------------------------
#
# 用户口径（2026-08-11）：「任务开始后，调度台固化任务数据，记录任务内容。
# 并且开始后，无法修改任务，只有结束状态才可以修改」。
#
# 为什么必须拒而不是收下：`_step()` 每秒重新去库里读一遍配置，收下的改动会
# **立刻**生效到下一轮，而上一轮正拿着旧参数在飞。一轮之内两套口径，事后从
# `mission_runs` 里只看得到一行命令行，分不出当时用的是哪一套。


def _start(console: Console) -> None:
    assert console.client.post("/api/scheduler/start").status_code == 200


def test_params_cannot_be_changed_while_the_scheduler_runs(console: Console) -> None:
    console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 5}})
    _start(console)

    response = console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 30}})

    assert response.status_code == 409, response.text
    assert "运行中" in response.json()["detail"]
    # 拒了就得真的没改。收下一个 409 却把值写进去，比静默忽略更糟。
    assert console.task("PIRATE")["params"] == {"radius": 5}


def test_priority_cannot_be_reordered_while_the_scheduler_runs(console: Console) -> None:
    """拖拽也走这个 PATCH，所以这一条同时守住了那个拖拽把手。"""
    _start(console)

    response = console.client.patch("/api/missions/BOT", json={"priority": -1})

    assert response.status_code == 409
    tasks = console.get()["tasks"]
    assert isinstance(tasks, list)
    assert tasks[0]["kind"] != "BOT"


def test_a_chain_cannot_be_switched_off_while_the_scheduler_runs(console: Console) -> None:
    """复选框也是任务配置的一部分：中途摘掉一条链路同样是「一轮之内两套口径」。"""
    _start(console)

    response = console.client.patch("/api/missions/SCAN", json={"enabled": False})

    assert response.status_code == 409
    assert console.task("SCAN")["enabled"] is True


def test_a_disabled_chain_can_still_be_revived_while_the_scheduler_runs(
    console: Console,
) -> None:
    """**「恢复」是运行中唯一的口子。**

    一条链路完全可能在调度器跑着的时候被自动停用（连崩三次，多半是「窗口抢不到
    前台」这类环境原因），而那正是用户最需要把它恢复回来的时刻。一刀切禁掉 PATCH
    的话，页面上那个「恢复」按钮就废了，用户只剩「点结束、恢复、再点开始」这一条
    路——代价是把另外两条正常的链路一起停掉。

    开这个口子不破坏固化：自动停用时 `enabled` **本来就还是 True**，
    `disabled_reason` 与失败计数是调度器自己的状态、不是用户填的配置，所以这一下
    不动固化记录里的任何一个字段。
    """
    _start(console)
    console.repository.record_mission_failure(MissionKind.SCAN, exit_code=1, limit=1)
    assert console.task("SCAN")["disabled_reason"] is not None

    response = console.client.patch("/api/missions/SCAN", json={"enabled": True})

    assert response.status_code == 200, response.text
    assert console.task("SCAN")["disabled_reason"] is None


def test_reviving_while_running_may_not_smuggle_in_a_param_change(console: Console) -> None:
    """口子只给「清停用状态」，不给「趁着恢复顺手改一笔」。"""
    console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 5}})
    _start(console)
    console.repository.disable_mission_task(MissionKind.PIRATE, "连续 3 次异常退出")

    response = console.client.patch(
        "/api/missions/PIRATE", json={"enabled": True, "params": {"radius": 30}}
    )

    assert response.status_code == 409
    assert console.task("PIRATE")["params"] == {"radius": 5}
    assert console.task("PIRATE")["disabled_reason"] is not None


def test_enabling_a_chain_that_is_not_disabled_is_still_refused(console: Console) -> None:
    """没被停用的行收到 `enabled: true` 不是「恢复」，是在勾一条没参与的链路。"""
    _start(console)

    response = console.client.patch("/api/missions/BOT", json={"enabled": True})

    assert response.status_code == 409


def test_the_configuration_is_editable_again_after_stopping(console: Console) -> None:
    """「只有结束状态才可以修改」的另一半：结束之后必须真的能改回来。"""
    _start(console)
    assert console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 9}}) is not None
    console.client.post("/api/scheduler/stop")

    response = console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 9}})

    assert response.status_code == 200, response.text
    assert console.task("PIRATE")["params"] == {"radius": 9}


def test_a_child_that_is_still_running_keeps_the_configuration_locked(console: Console) -> None:
    """「结束之后 runner 还在收尾」算不算停止状态：**只要手上还有子进程就不算。**

    正常路径上 `stop()` 是同步的（`terminate()` 之后 `wait()`），所以这一条永远
    不会拦住用户。这里直接把调度器的开关关掉、把子进程留在手上，钉住的是那个
    将来才会出现的窗口：哪天收尾改成异步的，锁必须自己跟着延长，而不是在收尾
    途中静默放行一次改参数。
    """
    _start(console)
    console.scheduler.tick()
    assert console.get()["current"] is not None

    console.scheduler._enabled = False  # noqa: SLF001 - 造出「关了但子进程还在」

    assert console.get()["config_locked"] is True
    refused = console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 7}})
    assert refused.status_code == 409


def test_a_new_bot_round_is_still_allowed_while_running(console: Console) -> None:
    """「重开一轮」不写任何一个配置字段，所以它不在这道锁里。

    它只把 `round_started_at_utc` 推到当前，也就是「按同一套配置再跑一遍」——
    固化记录里的每个字段都还是原样。挡掉它的话，用户要开新一轮就得先把整台
    调度器停下来。
    """
    _start(console)

    assert console.client.post("/api/missions/BOT/new-round").status_code == 200


# -- 配置固化 -------------------------------------------------------------------


def test_starting_freezes_the_configuration_of_that_moment(console: Console) -> None:
    """「开始」那一下抄一份，页面据此回答「这一轮到底按什么跑的」。"""
    console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 6}})
    _start(console)

    frozen = console.get()["frozen_config"]
    assert isinstance(frozen, dict)
    assert frozen["frozen_at_utc"].startswith("2026-08-09T12:00")
    pirate = next(task for task in frozen["tasks"] if task["kind"] == "PIRATE")
    assert pirate["params"] == {"radius": 6}
    assert pirate["summary"] == "半径 6"
    assert frozen["changes"] == ["首次记录"]


def test_a_stopped_scheduler_shows_no_frozen_configuration(console: Console) -> None:
    """停着的时候「本轮」不存在。把上一轮那份继续挂着会被读成「现在跑的就是这套」。"""
    _start(console)
    assert console.get()["frozen_config"] is not None

    console.client.post("/api/scheduler/stop")

    assert console.get()["frozen_config"] is None
    assert console.get()["config_locked"] is False


def test_the_second_start_records_what_changed_in_between(console: Console) -> None:
    """用户口径里的「记录任务内容」有两半，这是「改了什么、什么时候改的」那半。"""
    console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 5}})
    _start(console)
    console.client.post("/api/scheduler/stop")
    console.clock.now = NOW + timedelta(hours=1)
    console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 12}})
    console.client.patch("/api/missions/BOT", json={"priority": -1})
    _start(console)

    frozen = console.get()["frozen_config"]
    assert isinstance(frozen, dict)
    # 次序按链路（`MissionKind` 的声明顺序）走，不按改动发生的先后：两份记录
    # 逐条对比时，同一条链路必须落在同一格。
    assert frozen["changes"] == [
        "侦查+攻击海盗：半径 5 → 12",
        "扫描+攻击 bot：优先级 1 → -1",
    ]


def test_pressing_start_twice_does_not_record_a_second_freeze(console: Console) -> None:
    """第二下什么都没变，记下来只会把真正改过的那几条淹掉。"""
    _start(console)
    _start(console)

    assert len(console.scheduler.config_freezes()) == 1


def test_the_freeze_is_written_where_a_restarted_console_can_read_it(console: Console) -> None:
    """控制台重启后内存里的一切都没了，而用户多半是重启之后才来翻这份记录。"""
    console.client.patch("/api/missions/PIRATE", json={"params": {"radius": 4}})
    _start(console)

    lines = console.freeze_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    # 拿读回来的那一份断言，而不是在这行 JSON 上找子串：这条记录的用途就是
    # **被下一个进程读回来**，只断言「文件里有 radius 这几个字母」的话，一个
    # 存得下、读不回的格式照样能满足。
    reloaded = MissionFreezeLog(console.freeze_log).latest()
    assert reloaded is not None
    pirate = reloaded.task(MissionKind.PIRATE)
    assert pirate is not None
    assert json.loads(pirate.params_json) == {"radius": 4}


# -- 重开一轮 -------------------------------------------------------------------


def test_a_new_round_pushes_the_round_start_to_now(console: Console) -> None:
    """不推的话，上一轮打完的那批目标今天仍算「已完成」，新一轮永远开不起来。"""
    response = console.client.post("/api/missions/BOT/new-round")

    assert response.status_code == 200
    row = next(row for row in console.repository.mission_tasks() if row.kind == "BOT")
    assert row.round_started_at_utc == NOW


# -- 孤儿 -----------------------------------------------------------------------


def test_an_orphan_run_is_surfaced_with_its_pid(tmp_path: Path) -> None:
    """上次没走正常关闭路径留下的行，页面顶部要亮红条。

    pid 是给人拿去任务管理器里核对的，**不是给我们开枪用的**——pid 会被系统
    回收复用。
    """
    engine = create_database_engine(f"sqlite:///{tmp_path / 'orphan.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    repository = SqlAlchemyRepository(factory)
    repository.ensure_mission_rows(now_utc=NOW)
    repository.begin_mission_run(
        MissionKind.SCAN,
        command=["python"],
        pid=4321,
        started_at_utc=NOW - timedelta(hours=1),
        log_path="var/logs/mission-scan.log",
    )
    app = create_persistent_app(factory, local_token=TOKEN, tick_interval_s=3600.0)

    with TestClient(app, headers={"X-Evo-Helper-Token": TOKEN}) as client:
        assert client.get("/api/scheduler").json()["orphan_pid"] == 4321

        assert client.post("/api/scheduler/force-kill").status_code == 200

        body = client.get("/api/scheduler").json()
        assert body["orphan_pid"] is None
        assert body["running"] is False


def test_the_console_writes_its_freezes_under_var(tmp_path: Path) -> None:
    """默认的固化记录落在 `var/` 里，和子进程日志同一个地方。

    只建 app、不点「开始」，所以这条不会真的写出文件——`MissionFreezeLog` 是
    读构造、写追加的。钉住的是**接线**：控制台自己建调度器时必须给它一个带
    路径的账本，忘了给就只留在内存里，重启一次全没了，而那种丢失是静默的。
    """
    engine = create_database_engine(f"sqlite:///{tmp_path / 'wiring.db'}")
    Base.metadata.create_all(engine)
    app = create_persistent_app(
        create_session_factory(engine), local_token=TOKEN, tick_interval_s=3600.0
    )

    assert app.state.mission_scheduler.freeze_log_path == DEFAULT_FREEZE_LOG


def test_force_kill_stops_the_child_we_do_know_about(console: Console) -> None:
    """认识的那个进程照常停掉；不认识的 pid 一律不碰。"""
    console.client.post("/api/scheduler/start")
    console.scheduler.tick()

    console.client.post("/api/scheduler/force-kill")

    body = console.get()
    assert body["current"] is None
    assert body["running"] is False


# -- 桌面悬浮窗的契约 -----------------------------------------------------------


def test_the_desktop_window_can_read_this_endpoint(console: Console) -> None:
    """桌面悬浮窗（`tools/scan_console.py`）解的就是这个回包。

    悬浮窗那边只能拿假回包测自己，接口这边只能测自己的字段——两边都绿、
    形状却对不上，是这种「服务端下发、客户端照抄」的接口最典型的失效方式，
    而它在实机上表现为状态窗一直显示「未连接」，没有任何报错。
    所以在真接口的回包上跑一遍悬浮窗的解析器。
    """
    stopped = parse_scheduler(console.get())
    assert stopped.running is False
    assert stopped.current is None

    console.client.post("/api/scheduler/start")
    console.scheduler.tick()
    console.clock.now = NOW + timedelta(minutes=2)

    snapshot = parse_scheduler(console.get())
    assert snapshot.running is True
    assert snapshot.started_at_utc == NOW
    assert snapshot.current is not None
    # 链路名由服务端下发，悬浮窗不自己拼——两处各写一份就会有一天对不上。
    assert snapshot.current.label == "扫描全星系 bot"
    assert snapshot.current.started_at_utc == NOW


# -- 旧页面 ---------------------------------------------------------------------


def test_the_old_missions_page_still_renders(console: Console) -> None:
    """页面重做是下一个任务。这一步只加接口，不许把 `/missions` 弄崩。"""
    assert console.client.get("/missions").status_code == 200
    assert console.client.get("/logs").status_code == 200


def test_every_row_carries_a_revive_button(console: Console) -> None:
    """三条链路都可能被自动停用，恢复的入口就不能只长在其中一行上。

    显隐由 `status === '已停用'` 决定（脚本里），这里只钉住「按钮在每一行」——
    渲染在 `{% for %}` 外面写漏一次，页面上就再没有恢复的办法。
    """
    page = console.client.get("/missions").text

    assert page.count('class="btn small mission-revive"') == 3


# -- bot 命令行 ---------------------------------------------------------------


def test_the_launched_bot_command_has_no_probe_and_no_thresholds(console: Console) -> None:
    """runner 拿到的 argv 只有目标和 `--attack`。

    ⚠️ **多传一个参数的后果不是「多余」，是这条链路起不来**：`bot_loop` 的
    argparse 见到不认识的 `--probe` / `--tier-thresholds` 会 `SystemExit(2)`，
    而调度器看到的只是退出码非零、连撞三次就把整条 bot 链路自动停用。
    分档已经整套删除（用户口径 2026-08-13）。

    这条同时守着 `--attack` 必须在：漏掉它这一轮只会站过去看一眼，不报错、
    看着一切正常，而一发都没打。
    """
    _seed_bot(console.repository, Coordinate(2, 137, 14))
    console.client.patch(
        "/api/missions/BOT",
        json={"enabled": True, "params": {"galaxy": 2, "first_system": 130, "last_system": 140}},
    )
    console.client.patch("/api/missions/PIRATE", json={"enabled": False})
    console.client.patch("/api/missions/SCAN", json={"enabled": False})
    _start(console)
    console.scheduler.tick()

    command = next(
        item for item in console.launcher.commands if "evo_helper.tools.bot_loop" in item
    )

    assert "--attack" in command
    assert "--probe" not in command
    assert "--tier-thresholds" not in command


def test_the_tier_thresholds_api_is_gone(console: Console) -> None:
    """`/api/tier-thresholds` 与 `/tiers` 都不该再在了。

    留一条只读的旧接口比删掉更糟：页面上那三个框还能填、还能保存，而没有任何
    地方读它——用户会以为自己改了这一轮的打法。
    """
    assert console.client.get("/api/tier-thresholds").status_code == 404
    assert console.client.get("/tiers").status_code == 404
