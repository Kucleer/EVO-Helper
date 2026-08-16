"""系统日志的写入出口：有界队列 + 后台线程批量刷盘。

**为什么不能同步写库。** 生产库在 Tailscale 内网的另一台机器上（PostgreSQL），
一次 INSERT 就是一次网络往返。而这些日志的调用点全在实机点击循环里——海盗一轮
半小时、光 `say()` 就有 80 个调用点。把往返打进循环，等于给每一次点击加一段
时长不定的停顿，而 CLAUDE.md 的反行为检测那一条要求点击节奏必须是拟人化的随机
分布，不许出现固定卡顿。所以调用方只做一件事：把记录塞进内存队列，立刻返回。

**队列满了丢最旧的，绝不阻塞调用方。** 阻塞就是把网络故障变成实机卡死。丢最旧的
而不是丢最新的：出事那一刻的日志一定在队尾，那几条才是要看的。

**任何 DB 异常都在这里被吞掉。** 这不是防御性编程的口号，是有事故的：
`tools/scan_coordinates.py` 的 `say()` 上方记着 2026-08-10 那次——诊断路径上的
一行输出抛了异常，把整个 runner 崩在半路，级联成整条链路停摆。写库比 `print`
的失败面大得多（连不上、超时、库被锁、迁移没跑），一条都不许漏出去。写失败最多
往 stderr 打一条，而且要限流，否则库一断就刷屏，把真正的输出淹掉。

**进程退出要 flush。** 不 flush 的话，最后那批——也就是出事那一刻的日志——
正好还在队列里没落盘。`install_system_log_sink` 会注册 `atexit`。

实测（2026-08-16，本机 Windows / CPython 3.14，默认 capacity=5000、batch=200）：

- `emit` 两万次共 0.057 s，**2.9 µs/次**——调用线程完全不碰数据库。
- 写入器每批睡 50 ms（模拟内网往返）时，五千次 `emit` 共 0.014 s，丢 0 条：
  一轮实机的输出量在这个刷盘速度下根本填不满队列。
- 库**整个断线**（写入器每批都抛）时，两万次 `emit` 共 0.054 s，
  丢 11,880 条（全是最旧的）、41 批写入失败、stderr 上**只打了 1 行**（限流生效），
  一个异常都没有漏给调用方。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import socket
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

#: 队列上限。一轮海盗（半小时）大约几千条，这个深度足够扛住库那头断线几分钟；
#: 再大就只是把内存花在没人会看的陈年日志上。
DEFAULT_CAPACITY = 5000
#: 一次 INSERT 带多少条。批越大网络往返越少，但一次失败丢的也越多。
DEFAULT_BATCH_SIZE = 200
#: 队列空时后台线程的等待上限。有新记录时靠 `Condition.notify` 立刻醒，
#: 这个值只决定「关机信号没走到」时的最坏唤醒延迟。
DEFAULT_FLUSH_INTERVAL_S = 1.0
#: `close()` 等后台线程收尾的上限。等不到就放弃——退出路径上卡住比丢几条日志糟。
DEFAULT_CLOSE_TIMEOUT_S = 5.0
#: 写库失败往 stderr 报告的最小间隔。库断线时每批都报会刷屏。
ERROR_REPORT_INTERVAL_S = 60.0

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

#: 子进程从环境变量里认领自己的身份。调度器起 runner 时写进去（见
#: `child_environment`），手工直跑时这三个都不存在，于是 run_id 为空——
#: 「不属于任何一轮」本来就是它可空的理由。
ENV_RUN_ID = "EVO_HELPER_LOG_RUN_ID"
ENV_TASK_ID = "EVO_HELPER_LOG_TASK_ID"
ENV_MISSION_KIND = "EVO_HELPER_LOG_MISSION_KIND"

_HOST = socket.gethostname()[:64]


@dataclass(frozen=True, slots=True)
class SystemLogRecord:
    """一条待写入的日志。

    在**调用线程**上组装：机器名、pid、时刻都要是产生它的那一刻的事实，
    等后台线程再取就已经不是同一件事了（尤其 `logged_at_utc`）。
    """

    logged_at_utc: datetime
    level: str
    source: str
    host: str
    pid: int
    message: str
    run_id: UUID | None = None
    task_id: int | None = None
    mission_kind: str | None = None
    payload_json: str = "{}"


#: 写入后端。注入而不是直接依赖仓储：sink 的健壮性用例要能塞一个必然抛异常的
#: 写入器进来，那是这个模块最要紧的一条行为。
LogWriter = Callable[[Sequence[SystemLogRecord]], None]


@dataclass
class SystemLogContext:
    """当前进程属于哪一轮任务。runner 启动时定一次，之后不再变。"""

    run_id: UUID | None = None
    task_id: int | None = None
    mission_kind: str | None = None


def context_from_environment() -> SystemLogContext:
    """从环境变量认领本进程的身份；认不出来就当作「不属于任何一轮」。

    解析失败一律回落到 None 而不是报错：日志的身份认不出来是小事，
    为它把 runner 拦在启动阶段才是大事。
    """
    raw_run = os.environ.get(ENV_RUN_ID, "").strip()
    try:
        run_id = UUID(raw_run) if raw_run else None
    except ValueError:
        run_id = None
    raw_task = os.environ.get(ENV_TASK_ID, "").strip()
    task_id = int(raw_task) if raw_task.isdigit() else None
    kind = os.environ.get(ENV_MISSION_KIND, "").strip().lower() or None
    return SystemLogContext(run_id=run_id, task_id=task_id, mission_kind=kind)


@contextmanager
def child_environment(
    *, run_id: UUID | None, task_id: int | None, mission_kind: str | None
) -> Iterator[None]:
    """在 `os.environ` 上临时挂本轮身份，好让紧接着起的子进程继承过去。

    刻意**不改 `MissionSupervisor.start` / `launch_mission` 的签名**：那两个的
    `launch` 是注入点，测试里塞的假 launcher 都按现有的三个位置参数写着，
    多一个参数会把它们全部弄红——而这里要的只是「子进程能认出自己是哪一轮」。
    `Popen` 不传 `env` 时继承父进程环境，所以挂上再起就够了。

    退出时精确还原（原来没有就删掉），不留残值：残值会让下一次**手工直跑**的
    runner 认领上一轮的 run_id，而那正是这一列区分「有没有轮」的用途。
    """
    values = {
        ENV_RUN_ID: "" if run_id is None else str(run_id),
        ENV_TASK_ID: "" if task_id is None else str(task_id),
        ENV_MISSION_KIND: (mission_kind or "").lower(),
    }
    previous = {name: os.environ.get(name) for name in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old


@dataclass
class SinkStats:
    """实测这套策略到底丢没丢、写没写。页面上不显示，用例与排障看它。"""

    accepted: int = 0
    dropped: int = 0
    written: int = 0
    failed_batches: int = 0


class SystemLogSink:
    """把记录收进有界队列，由一个后台线程批量写出去。

    线程是 daemon：解释器不该为了一条日志而不肯退出。真正保证不丢的是
    `close()`（`atexit` 会调它），不是线程的存活。
    """

    def __init__(
        self,
        write: LogWriter,
        *,
        capacity: int = DEFAULT_CAPACITY,
        batch_size: int = DEFAULT_BATCH_SIZE,
        flush_interval_s: float = DEFAULT_FLUSH_INTERVAL_S,
        error_stream: Any = None,
        # 只给**限流**用（`_report`）。刷盘超时一律走真实的 `time.monotonic`，
        # 理由见 `flush` 的注释。
        clock: Callable[[], float] = time.monotonic,
        name: str = "evo-system-log",
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity 必须至少为 1")
        self._write = write
        self._batch_size = max(1, batch_size)
        self._flush_interval_s = flush_interval_s
        self._error_stream = error_stream
        self._clock = clock
        self._queue: deque[SystemLogRecord] = deque(maxlen=capacity)
        self._condition = threading.Condition()
        self._stopping = False
        self._closed = False
        self._inflight = 0
        self._last_error_at: float | None = None
        self._suppressed_errors = 0
        self.stats = SinkStats()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    # -- 写入侧（调用方线程）------------------------------------------------

    def emit(self, record: SystemLogRecord) -> None:
        """收下一条。**永不阻塞、永不抛异常。**

        队列满时 `deque(maxlen=…)` 自己把队头挤掉，所以这里只需要记一笔丢弃数；
        换成「满了就等」等于把库那头的故障变成实机的卡死。
        """
        try:
            with self._condition:
                if self._closed:
                    return
                if len(self._queue) == self._queue.maxlen:
                    self.stats.dropped += 1
                self._queue.append(record)
                self.stats.accepted += 1
                self._condition.notify()
        except Exception:  # noqa: BLE001 - 出口本身绝不许把调用方弄死
            pass

    def flush(self, timeout: float = DEFAULT_CLOSE_TIMEOUT_S) -> bool:
        """等到队列排空且当前那一批也写完。超时返回 False，不抛。

        ⚠️ 这里用的是 `time.monotonic` 而**不是**注入的 `self._clock`。那个时钟只
        服务于限流（用例要把 60 秒的窗口钉死才测得了），而钉死的时钟会让下面这个
        循环的 `remaining` 永远等于 `timeout`——也就是超时永远不发生。后台线程一旦
        没能把队列排空（比如它被一个异常弄死了），`flush` 就会永远挂在这里。
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._queue or self._inflight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
        return True

    def close(self, timeout: float = DEFAULT_CLOSE_TIMEOUT_S) -> None:
        """停止收新记录，把队列里剩下的写完再退。可重复调用。

        进程退出必须走这里：不走的话，最后那一批还躺在内存里，而那几条正是
        「它是怎么死的」。
        """
        with self._condition:
            if self._closed and self._stopping:
                return
            self._closed = True
            self._stopping = True
            self._condition.notify_all()
        self._thread.join(timeout)

    @property
    def pending(self) -> int:
        with self._condition:
            return len(self._queue) + self._inflight

    # -- 刷盘侧（后台线程）--------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    self._condition.wait(self._flush_interval_s)
                if not self._queue:
                    if self._stopping:
                        return
                    continue
                size = min(len(self._queue), self._batch_size)
                batch = [self._queue.popleft() for _ in range(size)]
                self._inflight = size
            self._write_batch(batch)
            with self._condition:
                self._inflight = 0
                self._condition.notify_all()

    def _write_batch(self, batch: Sequence[SystemLogRecord]) -> None:
        try:
            self._write(batch)
        except BaseException as error:  # noqa: BLE001 - 见模块注释里的那次事故
            self.stats.failed_batches += 1
            self._report(error)
        else:
            self.stats.written += len(batch)

    def _report(self, error: BaseException) -> None:
        """写库失败时往 stderr 打一条，**限流**。

        限流不是为了少打字：库断线时每一批都失败，不限流就是每秒几十行，把
        runner 真正的输出淹掉，而那份输出是本机唯一还看得见的东西。
        """
        try:
            now = self._clock()
            if self._last_error_at is not None and now - self._last_error_at < (
                ERROR_REPORT_INTERVAL_S
            ):
                self._suppressed_errors += 1
                return
            skipped = self._suppressed_errors
            self._last_error_at = now
            self._suppressed_errors = 0
            stream = self._error_stream if self._error_stream is not None else sys.stderr
            tail = f"（另有 {skipped} 批同样失败未逐条报告）" if skipped else ""
            print(
                f"[system-log] 日志写库失败，已丢弃这一批：{error!r}{tail}",
                file=stream,
                flush=True,
            )
        except Exception:  # noqa: BLE001 - 连报错都失败就彻底闭嘴
            pass


# -- 进程级出口 --------------------------------------------------------------

_STATE_LOCK = threading.Lock()
_SINK: SystemLogSink | None = None
_CONTEXT = SystemLogContext()


def install_system_log_sink(
    sink: SystemLogSink, *, context: SystemLogContext | None = None
) -> SystemLogSink:
    """装上进程级的日志出口，并注册退出时的 flush。装第二个会先关掉前一个。"""
    global _SINK, _CONTEXT
    with _STATE_LOCK:
        previous, _SINK = _SINK, sink
        _CONTEXT = context if context is not None else context_from_environment()
    if previous is not None:
        previous.close()
    atexit.register(sink.close)
    return sink


def shutdown_system_log_sink() -> None:
    """关掉当前出口并 flush。测试与显式关机路径用；`atexit` 也会兜一次。"""
    global _SINK
    with _STATE_LOCK:
        sink, _SINK = _SINK, None
    if sink is not None:
        sink.close()
        atexit.unregister(sink.close)


def current_system_log_sink() -> SystemLogSink | None:
    with _STATE_LOCK:
        return _SINK


def current_context() -> SystemLogContext:
    with _STATE_LOCK:
        return _CONTEXT


def record_system_log(
    level: str,
    source: str,
    message: str,
    *,
    payload: Mapping[str, Any] | None = None,
    logged_at_utc: datetime | None = None,
) -> None:
    """记一条系统日志。**没装出口时是空操作**，绝不抛异常。

    空操作是默认值而不是「自动连库」：`import` 一个工具模块不该在背地里建
    数据库连接，测试更不该。真正装上它的只有 runner 的 `main()` 与控制台进程。
    """
    sink = current_system_log_sink()
    if sink is None:
        return
    try:
        context = current_context()
        sink.emit(
            SystemLogRecord(
                logged_at_utc=logged_at_utc or datetime.now(UTC),
                level=_normalise_level(level),
                source=source[:64],
                host=_HOST,
                pid=os.getpid(),
                message=message,
                run_id=context.run_id,
                task_id=context.task_id,
                mission_kind=context.mission_kind,
                payload_json=encode_payload(payload),
            )
        )
    except Exception:  # noqa: BLE001 - 同 `emit`：诊断路径不许把调用方弄死
        pass


def encode_payload(payload: Mapping[str, Any] | None) -> str:
    """把附加信息编成 JSON 文本；编不出来的对象降级成它的 `repr`。

    `default=repr` 是刻意的：payload 里常有坐标、异常、`Path` 这类不可序列化的
    东西，而「因为一个字段编不出来就整条日志丢掉」正好丢在出事那一刻。
    """
    if not payload:
        return "{}"
    try:
        return json.dumps(dict(payload), ensure_ascii=False, default=repr)
    except Exception:  # noqa: BLE001
        return json.dumps({"payload_repr": repr(payload)}, ensure_ascii=False)


def _normalise_level(level: str) -> str:
    upper = level.upper()[:8]
    return upper if upper in LEVELS else "INFO"


# -- 标准库 logging 的桥 -----------------------------------------------------


class SystemLogHandler(logging.Handler):
    """把标准库日志灌进同一张表。

    装它之前，`application/mission_scheduler.py` 的两条 `_LOGGER.info` 与
    `vision/live_reports.py` 的 info **哪儿都到不了**——控制台进程从来没调过
    `infrastructure.logging.configure_logging()`，`evo_helper` 这棵 logger
    没有任何 handler，warning/exception 也只是被 `lastResort` 丢到 stderr。

    `source` 用 logger 名并去掉 `evo_helper.` 前缀，好和 `say()` 那条路写进去的
    `tools.bot_loop` 对齐——两条路进的是同一张表，`source` 的口径必须是一套。
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload: dict[str, Any] = {"logger": record.name, "func": record.funcName}
            if record.exc_info:
                payload["traceback"] = self.format(record) if self.formatter else _traceback(record)
            record_system_log(
                record.levelname,
                _source_of(record.name),
                record.getMessage(),
                payload=payload,
                logged_at_utc=datetime.fromtimestamp(record.created, UTC),
            )
        except Exception:  # noqa: BLE001 - handler 不许把被日志的那段代码弄死
            pass


def _traceback(record: logging.LogRecord) -> str:
    import traceback

    if record.exc_info is None:
        return ""
    return "".join(traceback.format_exception(*record.exc_info))


def _source_of(logger_name: str) -> str:
    trimmed = logger_name.removeprefix("evo_helper.")
    return (trimmed or logger_name)[:64]


@dataclass
class _AttachedHandler:
    """记住装上去的那个 handler，好在关机时摘干净（测试之间尤其要紧）。"""

    handler: SystemLogHandler | None = None
    logger: logging.Logger | None = None
    previous_level: int | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)


_ATTACHED = _AttachedHandler()


def attach_system_log_handler(
    *, logger_name: str = "evo_helper", level: int = logging.INFO
) -> SystemLogHandler:
    """把 DB handler 挂到 `evo_helper` 这棵 logger 上，并把级别放到 INFO。

    **只加 handler，不动已有的文件/控制台输出**：这是双写，不是搬家。
    级别必须一起设——不设的话根 logger 默认 WARNING，那两条 `_LOGGER.info`
    连 handler 都到不了，而它们恰恰是「调度器补认了几份战报」这种要看的事实。
    """
    detach_system_log_handler()
    handler = SystemLogHandler()
    handler.setLevel(level)
    logger = logging.getLogger(logger_name)
    with _ATTACHED._lock:
        _ATTACHED.previous_level = logger.level
        logger.setLevel(min(logger.level, level) if logger.level else level)
        logger.addHandler(handler)
        _ATTACHED.handler = handler
        _ATTACHED.logger = logger
    return handler


def detach_system_log_handler() -> None:
    with _ATTACHED._lock:
        handler, logger = _ATTACHED.handler, _ATTACHED.logger
        previous = _ATTACHED.previous_level
        _ATTACHED.handler = None
        _ATTACHED.logger = None
        _ATTACHED.previous_level = None
    if handler is not None and logger is not None:
        logger.removeHandler(handler)
        handler.close()
        if previous is not None:
            logger.setLevel(previous)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_CAPACITY",
    "ENV_MISSION_KIND",
    "ENV_RUN_ID",
    "ENV_TASK_ID",
    "LEVELS",
    "LogWriter",
    "SinkStats",
    "SystemLogContext",
    "SystemLogHandler",
    "SystemLogRecord",
    "SystemLogSink",
    "attach_system_log_handler",
    "child_environment",
    "context_from_environment",
    "current_context",
    "current_system_log_sink",
    "detach_system_log_handler",
    "encode_payload",
    "install_system_log_sink",
    "record_system_log",
    "shutdown_system_log_sink",
]
