"""runner 进程里，**标准库 `logging` 那条路也要进 `system_log`**。

仓里有两条输出路：`say()` / `warn()` 走 `record_system_log`，而
`vision/live_reports.py` 与 `vision/pirate_reports.py` 用的是标准库 `logging`。
四个 runner 从来没调过 `configure_logging()`，`evo_helper` 这棵 logger 一个
handler 都没有，所以在装上这座桥之前，第二条路上的每一句 warning 都只被
`logging.lastResort` 丢到那台机器的 stderr——**一条都进不了库**。

⚠️ 这不是理论问题。PR #165 给「获得资源没读全」加的那条 warning 就在第二条路上，
而它是「12 格全是 0」和「那一屏根本没读出来」唯一的分界证据：两者交出去的都是
空元组，日志不说，库里就永远分不开。实机在另一台机器上，stderr 与 `var/logs/`
跨机都取不到。

这份用例钉三件事，缺一件这座桥就等于没搭：

1. 装完之后，`evo_helper.*` 的 warning 真的落进出口。
2. **本机那份 stderr 副本还在**——把日志搬进库不该以本机那份变少为代价。
3. 装第二次不叠 handler（同一进程里重复调用是允许的）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from evo_helper.infrastructure.system_log import (
    SystemLogContext,
    SystemLogRecord,
    SystemLogSink,
    current_system_log_sink,
    detach_system_log_handler,
    install_system_log_sink,
    shutdown_system_log_sink,
)
from evo_helper.tools.runner_logging import (
    CONSOLE_HANDLER_NAME,
    attach_console_handler,
    install_runner_system_log,
)

#: 真实调用点之一（PR #165 的「获得资源没读全」就是从这里喊的）。
#: 用真名而不是随手编一个：`SystemLogHandler` 会把 logger 名去掉 `evo_helper.`
#: 前缀当成 `source`，而排障时正是按这个字段找人。
CALLER = "evo_helper.vision.live_reports"


class Collector:
    def __init__(self) -> None:
        self.records: list[SystemLogRecord] = []

    def __call__(self, batch) -> None:  # type: ignore[no-untyped-def]
        self.records.extend(batch)


@pytest.fixture(autouse=True)
def clean_logger() -> Iterator[None]:
    """用例之间把 `evo_helper` 这棵 logger 还原干净。

    这些 handler 是**进程级**的，漏一个就会串到别的用例上：那时红的是别人，
    而原因在这里。
    """
    logger = logging.getLogger("evo_helper")
    before = list(logger.handlers)
    level = logger.level
    try:
        yield
    finally:
        detach_system_log_handler()
        for handler in list(logger.handlers):
            if handler not in before:
                logger.removeHandler(handler)
        logger.setLevel(level)
        shutdown_system_log_sink()


@pytest.fixture
def collector() -> Iterator[Collector]:
    sink_collector = Collector()
    install_system_log_sink(
        SystemLogSink(sink_collector, flush_interval_s=0.01), context=SystemLogContext()
    )
    try:
        yield sink_collector
    finally:
        shutdown_system_log_sink()


def _flush() -> None:
    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)


def test_a_library_warning_from_a_runner_reaches_the_system_log(
    collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ **整份里最要紧的一条。**

    删掉 `install_runner_system_log` 里那句 `attach_system_log_handler()`，
    `vision` 那几条 warning 会一声不响地退回「只到 stderr」——生产库里查不到，
    而它们正是「战报资源整块没读出来」唯一的痕迹。

    出口用的是这条用例自己装的那个（`collector` fixture），所以库连不连得上
    与这里无关：这条钉的是**桥搭没搭**，不是数据库。
    """
    monkeypatch.setattr(
        "evo_helper.tools.runner_logging.install_database_system_log",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "evo_helper.tools.runner_logging.create_database_engine", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "evo_helper.tools.runner_logging.create_session_factory", lambda *args, **kwargs: None
    )
    install_runner_system_log()

    logging.getLogger(CALLER).warning(
        "战报 08:15:00 的「获得资源」没读全；这一份不记收获，也不补 0"
    )
    _flush()

    assert [entry.message for entry in collector.records] == [
        "战报 08:15:00 的「获得资源」没读全；这一份不记收获，也不补 0"
    ]
    assert collector.records[0].level == "WARNING"
    # `source` 去掉 `evo_helper.` 前缀，和 `say()` 那条路写进去的口径一致。
    assert collector.records[0].source == "vision.live_reports"


def test_the_local_stderr_copy_survives(
    collector: Collector, capsys: pytest.CaptureFixture[str]
) -> None:
    """本机那份不许变少。

    装上 DB handler 的那一刻 `logging.lastResort` 就不再兜底（它只在「一个
    handler 都没有」时才发话），所以本机那份必须由 `attach_console_handler`
    自己顶上——否则 runner 那台机器的 `var/logs/mission-*.log` 会静悄悄少掉
    这几条，而 `say()` 那条路一直是双写的。
    """
    attach_console_handler()

    logging.getLogger(CALLER).warning("这一句本机也要看得见")

    assert "这一句本机也要看得见" in capsys.readouterr().err


def test_info_stays_out_of_the_local_console() -> None:
    """本机那份只收 WARNING 及以上——原先 `lastResort` 就是这个级别。

    放到 INFO 等于给 runner 控制台凭空多出一堆从来没有过的行；入库那份仍是
    INFO，两份的取舍本来就不同。
    """
    attach_console_handler()

    handler = next(
        item
        for item in logging.getLogger("evo_helper").handlers
        if item.name == CONSOLE_HANDLER_NAME
    )
    assert handler.level == logging.WARNING


def test_attaching_twice_does_not_stack_handlers(
    collector: Collector, capsys: pytest.CaptureFixture[str]
) -> None:
    """同一进程里重复装是允许的，但一句话不许打两遍。

    叠起来的话，本机那份会一句变两句、库里那份也会重复入账——而重复的日志
    在事后翻账时和「真的发生了两次」长得一模一样。
    """
    attach_console_handler()
    attach_console_handler()

    logging.getLogger(CALLER).warning("只该出现一次")

    assert capsys.readouterr().err.count("只该出现一次") == 1
    assert (
        sum(
            1
            for item in logging.getLogger("evo_helper").handlers
            if item.name == CONSOLE_HANDLER_NAME
        )
        == 1
    )
