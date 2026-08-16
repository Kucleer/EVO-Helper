"""钉住 `SystemLogSink` 的三条硬约束。

这三条各自都有代价明确的反面，而且都**在绿测试里看不出来**：

1. 写库抛异常时调用方不受影响——反面是 2026-08-10 那次事故的重演：诊断路径上
   一行输出把整个 runner 崩在半路，级联成整条链路停摆。
2. 队列满了丢最旧的而不是阻塞——反面是把「库连不上」变成「实机点击卡死」，
   而 CLAUDE.md 明确要求点击节奏不许出现固定卡顿。
3. 退出前 flush——反面是最后那几条（也就是出事那一刻的）正好丢掉。

用注入的写入器而不是真库：这三条要验的是 sink 自己的行为，接真库只会让
「到底是谁吞了异常」变得说不清。
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest

from evo_helper.infrastructure.system_log import (
    SystemLogRecord,
    SystemLogSink,
)


def make_record(message: str) -> SystemLogRecord:
    return SystemLogRecord(
        logged_at_utc=datetime.now(UTC),
        level="INFO",
        source="tests.sink",
        host="test-host",
        pid=1234,
        message=message,
    )


class RecordingWriter:
    """记下每一批。可以被要求抛异常、或者卡在写入里不返回。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[SystemLogRecord]] = []
        self.fail = fail
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, batch) -> None:  # type: ignore[no-untyped-def]
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail:
            raise RuntimeError("库连不上")
        with self._lock:
            self.batches.append(list(batch))

    @property
    def messages(self) -> list[str]:
        with self._lock:
            return [record.message for batch in self.batches for record in batch]


# -- 1. 写库失败不许伤到调用方 -----------------------------------------------


def test_a_failing_writer_never_reaches_the_caller() -> None:
    """写入器每一批都抛，`emit` 仍然是正常返回。

    这条是这个模块存在的理由。它红了就意味着一次数据库抖动可以把实机 runner
    连同它手上那一轮一起弄死。
    """
    errors: list[str] = []

    class Stream:
        def write(self, text: str) -> int:
            errors.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    sink = SystemLogSink(RecordingWriter(fail=True), error_stream=Stream(), flush_interval_s=0.01)
    try:
        for index in range(20):
            sink.emit(make_record(f"第 {index} 条"))  # 不抛就是通过
        assert sink.flush(timeout=5)
    finally:
        sink.close()

    assert sink.stats.failed_batches >= 1, "写入器明明一直在抛，却一批失败都没记到"
    assert sink.stats.written == 0
    # 失败要有人知道，但只能是 stderr 上限流的一条，不能是抛给调用方。
    assert any("日志写库失败" in text for text in errors)


def test_repeated_failures_are_throttled_on_stderr() -> None:
    """库断线时不许刷屏——刷屏会把 runner 真正的输出淹掉。"""
    lines: list[str] = []

    class Stream:
        def write(self, text: str) -> int:
            if text.strip():
                lines.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    # 时钟钉死：限流窗口是 60 秒，真实时钟下这条用例要跑一分钟才有意义。
    sink = SystemLogSink(
        RecordingWriter(fail=True),
        error_stream=Stream(),
        batch_size=1,
        flush_interval_s=0.01,
        clock=lambda: 100.0,
    )
    try:
        for index in range(10):
            sink.emit(make_record(f"第 {index} 条"))
        sink.flush(timeout=5)
    finally:
        sink.close()

    assert sink.stats.failed_batches >= 5, "批大小是 1，十条应该失败十批"
    assert len(lines) == 1, f"限流失效，stderr 上打了 {len(lines)} 条"


# -- 2. 队列满了丢最旧的，且绝不阻塞 -----------------------------------------


def _emit_all(sink: SystemLogSink, messages: list[str]) -> threading.Thread:
    """在**另一个线程**上灌一批，返回那个线程。

    ⚠️ 不在测试线程上直接灌：`emit` 一旦退化成「满了就等」，直接灌会让这个用例
    死锁在半路（松开写入器的那句在后面），而死锁只会挂住整套测试，不会给出一条
    红色的断言。放到线程上，就能用 `join(timeout)` 把「有没有等」变成一句断言。
    """
    thread = threading.Thread(
        target=lambda: [sink.emit(make_record(text)) for text in messages], daemon=True
    )
    thread.start()
    return thread


def test_a_full_queue_drops_the_oldest_and_keeps_the_newest() -> None:
    """写入器卡住、队列被塞满时，留在队列里的必须是**最新**那几条。

    出事那一刻的日志一定在队尾。丢最新的等于把唯一有用的那几条丢掉。
    """
    writer = RecordingWriter()
    gate = threading.Event()
    writer.gate = gate
    sink = SystemLogSink(writer, capacity=3, batch_size=1, flush_interval_s=0.01)
    try:
        sink.emit(make_record("occupy"))  # 这一条会被后台线程取走并卡在写入里
        assert writer.entered.wait(5), "后台线程没有开始写"
        filler = _emit_all(sink, [f"m{index}" for index in range(10)])
        filler.join(3)
        assert not filler.is_alive(), "队列满了之后 emit 挂住了：应该丢最旧的，不是等"
        assert sink.stats.dropped == 7, f"丢弃数不对：{sink.stats.dropped}"
        gate.set()
        assert sink.flush(timeout=5)
    finally:
        gate.set()
        sink.close()

    # 队列容量 3，最后写出去的必须是最新的三条，最旧的 m0–m6 被挤掉。
    assert writer.messages == ["occupy", "m7", "m8", "m9"]


def test_emit_returns_immediately_even_while_the_writer_is_stuck() -> None:
    """写入器卡死时 `emit` 仍然立刻返回。

    ⚠️ 这条**不是**在测「快不快」，是在测「有没有等」：把队列换成
    `queue.Queue(maxsize=…).put()`（满了就阻塞）之后，下面这一百次调用会一直
    挂到写入器松手为止，也就是把库那头的故障直接变成实机的卡死。
    """
    writer = RecordingWriter()
    gate = threading.Event()
    writer.gate = gate
    sink = SystemLogSink(writer, capacity=2, batch_size=1, flush_interval_s=0.01)
    try:
        sink.emit(make_record("occupy"))
        assert writer.entered.wait(5)
        started = time.monotonic()
        flood = _emit_all(sink, [f"m{index}" for index in range(100)])
        flood.join(2)
        elapsed = time.monotonic() - started
        assert not flood.is_alive(), "emit 被满队列挡住了，一直没返回"
    finally:
        gate.set()
        sink.close()

    # 写入器要卡 5 秒（`gate.wait(5)`）。真阻塞的话这里至少是那个量级。
    assert elapsed < 1.0, f"emit 被队列挡住了：100 次用了 {elapsed:.3f}s"


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError):
        SystemLogSink(RecordingWriter(), capacity=0)


# -- 3. 关闭时把剩下的写完 ----------------------------------------------------


def test_close_flushes_what_is_still_queued() -> None:
    """`close()` 之前塞进去的必须全部落盘。

    进程退出时 `atexit` 调的就是它。不 flush 的话丢掉的正好是最后那几条——
    也就是「它是怎么死的」。
    """
    writer = RecordingWriter()
    # 刷盘间隔调得很长：如果 close 不主动排空，后台线程根本来不及自己醒过来写。
    sink = SystemLogSink(writer, batch_size=2, flush_interval_s=30.0)
    for index in range(7):
        sink.emit(make_record(f"m{index}"))

    sink.close(timeout=5)

    assert writer.messages == [f"m{index}" for index in range(7)]
    assert sink.stats.written == 7
    assert sink.pending == 0


def test_emit_after_close_is_ignored_rather_than_raising() -> None:
    """关掉之后再来一条，也只是被无视——退出路径上抛异常同样会弄死进程。"""
    writer = RecordingWriter()
    sink = SystemLogSink(writer, flush_interval_s=0.01)
    sink.close(timeout=5)

    sink.emit(make_record("迟到的一条"))

    assert writer.messages == []


def test_close_is_idempotent() -> None:
    """`atexit` 与显式关机路径都会调它，调两次不许出事。"""
    sink = SystemLogSink(RecordingWriter(), flush_interval_s=0.01)
    sink.close(timeout=5)
    sink.close(timeout=5)


# -- 并发：多个线程同时写 -----------------------------------------------------


def test_concurrent_emitters_lose_nothing_when_the_queue_is_deep_enough() -> None:
    """八个线程各写 100 条，队列够深时一条都不该丢或串。

    实机那头只有一个线程在 `say()`，但控制台进程里 tick 循环、请求处理与
    `logging` 桥是并发的，同一个 sink 会被好几个线程同时喂。
    """
    writer = RecordingWriter()
    sink = SystemLogSink(writer, capacity=2000, batch_size=50, flush_interval_s=0.01)

    def worker(index: int) -> None:
        for step in range(100):
            sink.emit(make_record(f"t{index}-{step}"))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    sink.close(timeout=10)

    assert sink.stats.dropped == 0
    assert len(writer.messages) == 800
    assert len(set(writer.messages)) == 800
