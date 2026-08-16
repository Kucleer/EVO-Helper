"""进程级出口、身份认领、以及标准库 logging 的桥。"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest

from evo_helper.infrastructure.system_log import (
    ENV_MISSION_KIND,
    ENV_RUN_ID,
    ENV_TASK_ID,
    SystemLogContext,
    SystemLogRecord,
    SystemLogSink,
    attach_system_log_handler,
    child_environment,
    context_from_environment,
    detach_system_log_handler,
    encode_payload,
    install_system_log_sink,
    record_system_log,
    shutdown_system_log_sink,
)


class Collector:
    def __init__(self) -> None:
        self.records: list[SystemLogRecord] = []

    def __call__(self, batch) -> None:  # type: ignore[no-untyped-def]
        self.records.extend(batch)


@pytest.fixture
def collector() -> Iterator[Collector]:
    """装一个进程级出口，用例结束一定摘掉。

    不摘的话下一个用例会往上一个用例的 sink 里写——而这类串味只在整套测试
    一起跑时才显形，单跑永远是绿的。
    """
    sink_collector = Collector()
    install_system_log_sink(
        SystemLogSink(sink_collector, flush_interval_s=0.01),
        context=SystemLogContext(),
    )
    try:
        yield sink_collector
    finally:
        shutdown_system_log_sink()


def _flush() -> None:
    from evo_helper.infrastructure.system_log import current_system_log_sink

    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)


def test_record_without_a_sink_is_a_no_op() -> None:
    """没装出口时什么都不做，尤其不抛。

    `import` 一条工具链路不该在背地里连库；测试更不该。默认关着是这个设计的
    前提，不是巧合。
    """
    shutdown_system_log_sink()
    record_system_log("INFO", "tests.entry", "没人接这一条")  # 不抛就是通过


def test_a_recorded_line_carries_host_and_pid(collector: Collector) -> None:
    """机器名与 pid 是跨机查看的刚需，必须由出口自己填上。"""
    record_system_log("info", "tests.entry", "你好")
    _flush()

    assert len(collector.records) == 1
    entry = collector.records[0]
    assert entry.message == "你好"
    assert entry.level == "INFO", "级别没有归一成大写"
    assert entry.host and entry.pid > 0
    assert entry.logged_at_utc.tzinfo is not None


def test_an_unknown_level_falls_back_to_info(collector: Collector) -> None:
    """列宽只有 8，而且页面按四档着色。认不出的档落到 INFO，不许把行丢掉。"""
    record_system_log("TRACE", "tests.entry", "?")
    _flush()

    assert collector.records[0].level == "INFO"


def test_the_context_is_stamped_onto_every_line(collector: Collector) -> None:
    run_id = uuid4()
    install_system_log_sink(
        SystemLogSink(collector, flush_interval_s=0.01),
        context=SystemLogContext(run_id=run_id, task_id=7, mission_kind="bot"),
    )
    record_system_log("INFO", "tests.entry", "一轮里的一句")
    _flush()

    entry = collector.records[-1]
    assert (entry.run_id, entry.task_id, entry.mission_kind) == (run_id, 7, "bot")


def test_child_environment_round_trips_through_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调度器挂上身份 → 子进程认领得到；退出之后一点残值都不留。

    残值最要命：留着的话，下一次**手工直跑**的 runner 会认领上一轮的 run_id，
    而「属不属于某一轮」正是这一列的全部意义。
    """
    monkeypatch.delenv(ENV_RUN_ID, raising=False)
    monkeypatch.delenv(ENV_TASK_ID, raising=False)
    monkeypatch.delenv(ENV_MISSION_KIND, raising=False)
    run_id = uuid4()

    with child_environment(run_id=run_id, task_id=3, mission_kind="PIRATE"):
        inside = context_from_environment()

    assert inside == SystemLogContext(run_id=run_id, task_id=3, mission_kind="pirate")
    assert context_from_environment() == SystemLogContext()


def test_a_broken_run_id_in_the_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """认不出的身份当作「没有」，不许把 runner 拦在启动阶段。"""
    monkeypatch.setenv(ENV_RUN_ID, "not-a-uuid")
    monkeypatch.setenv(ENV_TASK_ID, "abc")
    monkeypatch.setenv(ENV_MISSION_KIND, "")

    assert context_from_environment() == SystemLogContext()


def test_payload_encoding_survives_unserialisable_values() -> None:
    """编不出来的字段降级成 repr，而不是把整条日志丢掉。

    payload 里常有坐标、异常、`Path`。丢掉的那一条往往正是出事那一条。
    """

    class Odd:
        def __repr__(self) -> str:
            return "<odd>"

    assert encode_payload(None) == "{}"
    assert "<odd>" in encode_payload({"thing": Odd()})
    # 中文不许被转义成 \uXXXX——这份 JSON 是要在页面上直接给人看的。
    assert "坐标" in encode_payload({"坐标": "2:137:1"})


# -- 标准库 logging 的桥 -----------------------------------------------------


def test_the_handler_feeds_standard_library_logs_into_the_same_table(
    collector: Collector,
) -> None:
    """`_LOGGER.info` 要能进库。

    装它之前，`evo_helper` 这棵 logger 一个 handler 都没有——控制台进程从来
    没调过 `configure_logging()`，所以 `mission_scheduler` 的两条 info
    此前哪儿都到不了。
    """
    attach_system_log_handler()
    try:
        logging.getLogger("evo_helper.application.mission_scheduler").info("补认 3 份战报")
        _flush()
    finally:
        detach_system_log_handler()

    entry = collector.records[-1]
    assert entry.message == "补认 3 份战报"
    assert entry.level == "INFO"
    # `source` 与 `say()` 那条路写进去的 `tools.bot_loop` 是一套口径。
    assert entry.source == "application.mission_scheduler"


def test_the_handler_keeps_existing_handlers(collector: Collector) -> None:
    """这是**双写**，不是搬家：原有的文件/控制台 handler 一个都不许掉。"""
    logger = logging.getLogger("evo_helper")
    existing = logging.NullHandler()
    logger.addHandler(existing)
    try:
        attach_system_log_handler()
        assert existing in logger.handlers
    finally:
        detach_system_log_handler()
        logger.removeHandler(existing)


def test_detaching_restores_the_previous_level(collector: Collector) -> None:
    """摘掉之后级别要还原，否则一次装载会永久改掉整棵 logger 的行为。"""
    logger = logging.getLogger("evo_helper")
    logger.setLevel(logging.CRITICAL)
    try:
        attach_system_log_handler(level=logging.INFO)
        assert logger.level == logging.INFO
        detach_system_log_handler()
        assert logger.level == logging.CRITICAL
    finally:
        logger.setLevel(logging.NOTSET)


def test_an_exception_log_carries_its_traceback(collector: Collector) -> None:
    """异常栈要跟着进 payload——出事时唯一说得清「为什么」的就是它。"""
    attach_system_log_handler()
    try:
        try:
            raise RuntimeError("认不出画面")
        except RuntimeError:
            logging.getLogger("evo_helper.tools.pirate_loop").exception("这一发不派")
        _flush()
    finally:
        detach_system_log_handler()

    entry = collector.records[-1]
    assert entry.level == "ERROR"
    assert "RuntimeError" in entry.payload_json
    assert "认不出画面" in entry.payload_json


def test_a_broken_sink_cannot_break_the_logging_call(collector: Collector) -> None:
    """出口整个坏掉时，`_LOGGER.info` 本身也不许抛。"""

    class Exploding:
        def emit(self, record: SystemLogRecord) -> None:
            raise RuntimeError("出口炸了")

        def close(self, timeout: float = 0.0) -> None:
            return None

    install_system_log_sink(Exploding(), context=SystemLogContext())  # type: ignore[arg-type]
    attach_system_log_handler()
    try:
        logging.getLogger("evo_helper.tests").info("照常返回")  # 不抛就是通过
    finally:
        detach_system_log_handler()


def test_uuid_context_defaults_to_none_when_nothing_is_set() -> None:
    assert SystemLogContext().run_id is None
    assert isinstance(uuid4(), UUID)
