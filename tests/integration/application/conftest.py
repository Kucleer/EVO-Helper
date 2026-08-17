"""调度循环的集成夹具：真库 + 假子进程。

**这里的 `launch` 永远是假的。** 真的 `Popen` 一个 runner 会去点真实鼠标、
派真实舰队，在 CI 上尤其不能发生。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.models import RunInstance, ScanPlan
from evo_helper.storage.repository import SqlAlchemyRepository
from support.database import scratch_database_url


class FakeProcess:
    def __init__(self, kind: MissionKind, command: Sequence[str], pid: int) -> None:
        self.kind = kind
        self.command = list(command)
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
    """记下每次「起进程」的种类与命令行，好让测试断言起了谁、带了什么参数。"""

    def __init__(self) -> None:
        self.spawned: list[FakeProcess] = []

    def __call__(self, kind: MissionKind, command: Sequence[str], log_path: Path) -> FakeProcess:
        process = FakeProcess(kind, command, pid=7000 + len(self.spawned))
        self.spawned.append(process)
        return process

    @property
    def kinds(self) -> list[MissionKind]:
        return [process.kind for process in self.spawned]

    @property
    def latest(self) -> FakeProcess:
        return self.spawned[-1]


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_database_engine(scratch_database_url(tmp_path, "scheduler.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def repository(session_factory: sessionmaker[Session]) -> SqlAlchemyRepository:
    return SqlAlchemyRepository(session_factory)


@pytest.fixture
def launcher() -> FakeLauncher:
    return FakeLauncher()


@pytest.fixture
def run_id(session_factory: sessionmaker[Session]):  # type: ignore[no-untyped-def]
    """攻击意图的 `run_id` 是指向 `run_instances` 的外键，不能随手编一个。"""
    created = datetime.now(UTC)
    with session_factory() as session:
        plan = ScanPlan(name="scheduler-loop", created_at_utc=created)
        session.add(plan)
        session.flush()
        run = RunInstance(
            plan_id=plan.id,
            idempotency_key="scheduler-loop-001",
            state="SCANNING",
            created_at_utc=created,
        )
        session.add(run)
        session.commit()
        return run.id


def make_supervisor(launcher: FakeLauncher, clock: Clock) -> MissionSupervisor:
    return MissionSupervisor(launch=launcher, clock=clock)
