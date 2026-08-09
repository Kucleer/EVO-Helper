"""调度台的 API。

写请求只有同源校验（局域网内浏览器天然同源），所以这里只测行为，不测鉴权——
那是 `web/security.py` 的事。

**这里不真的 Popen 任何 runner**：`launch` 一律注入假的。真起一个会去点用户的
真实鼠标、派真实舰队。后台 tick 也被推到一小时一次，免得测试里冒出计划外的启动。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
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
    scheduler = MissionScheduler(repository, supervisor, clock=clock)
    app = create_persistent_app(
        factory,
        local_token=TOKEN,
        mission_scheduler=scheduler,
        # 后台 tick 先 sleep 再 tick，推到一小时就等于「测试期间不会自己跑」。
        tick_interval_s=3600.0,
    )
    with TestClient(app, headers={"X-Evo-Helper-Token": TOKEN}) as client:
        yield Console(client, repository, scheduler, launcher, clock)


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


def test_an_unknown_kind_is_a_404(console: Console) -> None:
    assert console.client.patch("/api/missions/DRAGON", json={"enabled": True}).status_code == 404


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


def test_force_kill_stops_the_child_we_do_know_about(console: Console) -> None:
    """认识的那个进程照常停掉；不认识的 pid 一律不碰。"""
    console.client.post("/api/scheduler/start")
    console.scheduler.tick()

    console.client.post("/api/scheduler/force-kill")

    body = console.get()
    assert body["current"] is None
    assert body["running"] is False


# -- 旧页面 ---------------------------------------------------------------------


def test_the_old_missions_page_still_renders(console: Console) -> None:
    """页面重做是下一个任务。这一步只加接口，不许把 `/missions` 弄崩。"""
    assert console.client.get("/missions").status_code == 200
    assert console.client.get("/logs").status_code == 200
