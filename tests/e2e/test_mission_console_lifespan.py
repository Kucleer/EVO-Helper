"""控制台进程的生命周期：开机补行、标孤儿，关机清子进程。

**这里不真的 Popen 任何 runner**——注入假的 `launch`。真起一个会去点用户的
真实鼠标、派真实舰队。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exit_code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code if self.exit_code is not None else 0


class FakeLauncher:
    def __init__(self) -> None:
        self.spawned: list[FakeProcess] = []

    def __call__(self, kind: MissionKind, command: Sequence[str], log_path: Path) -> FakeProcess:
        process = FakeProcess(pid=8000 + len(self.spawned))
        self.spawned.append(process)
        return process


def build(tmp_path: Path):  # type: ignore[no-untyped-def]
    engine = create_database_engine(f"sqlite:///{tmp_path / 'console.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    return factory, SqlAlchemyRepository(factory)


def test_startup_seeds_every_chain(tmp_path: Path) -> None:
    """迁移里没有 `bulk_insert`，少一行只会让那条链路凭空消失在调度台上。"""
    factory, repository = build(tmp_path)
    app = create_persistent_app(factory, local_token="t")

    with TestClient(app):
        pass

    assert sorted(row.kind for row in repository.mission_tasks()) == [
        "BOT",
        "PIRATE",
        "RANKING",
        "SCAN",
    ]


def test_startup_marks_orphans_without_shooting_at_a_recycled_pid(tmp_path: Path) -> None:
    """上次没走正常关闭路径留下的行标成 UNKNOWN，页面据此亮红条。

    **不按 pid 自动杀。** pid 会被系统回收复用，照着一个可能已经换了主人的
    号码开枪，比留个警告糟得多。
    """
    factory, repository = build(tmp_path)
    repository.ensure_mission_rows(now_utc=NOW)
    repository.begin_mission_run(
        MissionKind.SCAN,
        task_id=next(
            row.id for row in repository.mission_tasks() if row.kind == MissionKind.SCAN.value
        ),
        command=["python"],
        pid=31337,
        started_at_utc=NOW - timedelta(hours=2),
        log_path="var/logs/mission-scan.log",
    )
    app = create_persistent_app(factory, local_token="t")

    with TestClient(app):
        pass

    row = repository.mission_runs(limit=1)[0]
    assert row.stopped_by == "UNKNOWN"
    assert row.pid == 31337


def test_the_scheduler_comes_up_stopped(tmp_path: Path) -> None:
    """控制台重启后一律停在「已停止」——重启多半意味着出了事。"""
    factory, _ = build(tmp_path)
    app = create_persistent_app(factory, local_token="t")

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert not app.state.mission_scheduler.enabled


def test_shutdown_kills_the_child_so_it_does_not_outlive_the_console(tmp_path: Path) -> None:
    """不清场的话，控制台关了，一个还在点鼠标的 runner 留在后台。"""
    factory, repository = build(tmp_path)
    launcher = FakeLauncher()
    scheduler = MissionScheduler(
        repository, MissionSupervisor(launch=launcher, clock=lambda: NOW), clock=lambda: NOW
    )
    app = create_persistent_app(factory, local_token="t", mission_scheduler=scheduler)

    with TestClient(app):
        pirate = next(
            row.id for row in repository.mission_tasks() if row.kind == MissionKind.PIRATE.value
        )
        repository.update_mission_task(pirate, enabled=True)
        scheduler.start()
        scheduler.tick()
        assert len(launcher.spawned) == 1

    assert launcher.spawned[0].terminated
    assert repository.mission_runs(limit=1)[0].stopped_by == "SHUTDOWN"
