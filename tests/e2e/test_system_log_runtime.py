"""控制台进程起来时，系统日志的三件事要真的接上。

1. 出口装上了——否则页面永远是空的，而「空」读起来就是「实机没在跑」。
2. 标准库日志进得了库——`mission_scheduler` 的 `_LOGGER.info` 在这之前
   **哪儿都到不了**：控制台从来没调过 `configure_logging()`。
3. 保留期清理在开机时跑过一次——这是本进程唯一现成的周期性时机。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from evo_helper.config import Settings
from evo_helper.infrastructure.system_log import (
    SystemLogRecord,
    current_system_log_sink,
    detach_system_log_handler,
    shutdown_system_log_sink,
)
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.system_log import SystemLogRepository
from evo_helper.web.runtime import create_runtime_app
from support.database import scratch_database_url


@pytest.fixture(autouse=True)
def clean_process_state() -> Iterator[None]:
    """进程级的出口与 handler 都是全局的，用例之间必须摘干净。

    不摘的话下一个用例会往这一个的 sink 里写，而这类串味只在整套一起跑时显形。
    """
    yield
    detach_system_log_handler()
    shutdown_system_log_sink()


def _old(message: str, days: int) -> SystemLogRecord:
    return SystemLogRecord(
        logged_at_utc=datetime.now(UTC) - timedelta(days=days),
        level="INFO",
        source="tests.runtime",
        host="seed-host",
        pid=1,
        message=message,
    )


def test_the_console_installs_the_sink_and_the_logging_bridge(tmp_path: Path) -> None:
    database_url = scratch_database_url(tmp_path, "runtime-log.db")

    create_runtime_app(Settings(database_url=database_url), local_token="runtime-token")

    sink = current_system_log_sink()
    assert sink is not None, "控制台起来了却没有日志出口"
    logging.getLogger("evo_helper.application.mission_scheduler").info("启动调度前补认 3 份战报")
    assert sink.flush(timeout=5)
    sink.close(timeout=5)

    logs = SystemLogRepository(create_session_factory(create_database_engine(database_url))).query()
    assert [row.message for row in logs.rows] == ["启动调度前补认 3 份战报"]
    assert logs.rows[0].source == "application.mission_scheduler"


def test_startup_purges_logs_past_the_retention_window(tmp_path: Path) -> None:
    database_url = scratch_database_url(tmp_path, "retention.db")
    # 先把表建出来（迁移会在 `create_runtime_app` 里跑一次；这里手动跑一遍
    # 才能在启动之前塞进旧行）。
    create_runtime_app(Settings(database_url=database_url), local_token="t")
    shutdown_system_log_sink()
    session_factory = create_session_factory(create_database_engine(database_url))
    repository = SystemLogRepository(session_factory)
    repository.append([_old("三十天前", 30), _old("一天前", 1)])

    create_runtime_app(
        Settings(database_url=database_url, system_log_retention_days=14), local_token="t"
    )

    assert [row.message for row in repository.query().rows] == ["一天前"]


def test_zero_retention_keeps_everything(tmp_path: Path) -> None:
    """0 是「不清理」，不是「全删」——手滑一个配置值不该清空事后唯一能翻的东西。"""
    database_url = scratch_database_url(tmp_path, "retention-off.db")
    create_runtime_app(Settings(database_url=database_url), local_token="t")
    shutdown_system_log_sink()
    repository = SystemLogRepository(create_session_factory(create_database_engine(database_url)))
    repository.append([_old("九百天前", 900)])

    create_runtime_app(
        Settings(database_url=database_url, system_log_retention_days=0), local_token="t"
    )

    assert repository.query().total == 1
