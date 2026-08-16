"""调度器起 runner 时，本轮身份要通过环境变量传给子进程。

没有这一步，实机那台机器上写进 `system_log` 的每一行都会落成「不属于任何一轮」，
而 `(run_id, id)` 那条索引与页面上的「轮次」筛选也就全是空的。

⚠️ 身份必须在**起子进程之前**就定下来。`begin_mission_run` 原本是起完之后才
生成 id 的，照那个顺序传不过去。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.scheduler import MissionKind
from evo_helper.infrastructure.system_log import (
    ENV_MISSION_KIND,
    ENV_RUN_ID,
    ENV_TASK_ID,
    context_from_environment,
)
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, FakeLauncher, FakeProcess, make_supervisor

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class EnvSnoopingLauncher(FakeLauncher):
    """假 launcher，顺手把「起进程那一刻」的环境变量抄一份。

    真的 `Popen` 不传 `env` 时继承父进程环境，所以这三个变量在这一刻的取值
    就是子进程将会读到的取值。
    """

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[dict[str, str]] = []

    def __call__(self, kind: MissionKind, command: Sequence[str], log_path: Path) -> FakeProcess:
        self.seen.append(
            {name: os.environ.get(name, "") for name in (ENV_RUN_ID, ENV_TASK_ID, ENV_MISSION_KIND)}
        )
        return super().__call__(kind, command, log_path)


@pytest.fixture
def launcher() -> EnvSnoopingLauncher:
    return EnvSnoopingLauncher()


@pytest.fixture
def scheduler(repository, launcher) -> MissionScheduler:  # type: ignore[no-untyped-def]
    clock = Clock(NOW)
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


def _enable(repository: SqlAlchemyRepository, kind: MissionKind) -> int:
    task_id = next(row.id for row in repository.mission_tasks() if row.kind == kind.value)
    repository.update_mission_task(task_id, enabled=True)
    return task_id


def test_the_child_inherits_the_run_it_belongs_to(scheduler, repository, launcher) -> None:  # type: ignore[no-untyped-def]
    task_id = _enable(repository, MissionKind.PIRATE)
    scheduler.start()

    scheduler.tick()

    seen = launcher.seen[0]
    row = repository.mission_runs(limit=1)[0]
    assert UUID(seen[ENV_RUN_ID]) == row.id, "子进程拿到的 run_id 与账本上那一行对不上"
    assert seen[ENV_TASK_ID] == str(task_id)
    assert seen[ENV_MISSION_KIND] == "pirate"


def test_the_parent_environment_is_left_clean(scheduler, repository, launcher) -> None:  # type: ignore[no-untyped-def]
    """起完就还原。残值会让下一次**手工直跑**的 runner 认领上一轮的 run_id。"""
    _enable(repository, MissionKind.PIRATE)
    scheduler.start()

    scheduler.tick()

    assert context_from_environment().run_id is None
    assert os.environ.get(ENV_RUN_ID) is None
