"""把 `SystemLogSink` 接到真正的数据库上，并提供保留期清理。

单独一个模块而不是塞进 `infrastructure/system_log.py`：那一个不许知道存储层的
存在（`storage/system_log.py` 反过来 import 它），这一个才是两边的装配点。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session, sessionmaker

from evo_helper.storage.system_log import SystemLogRepository

from .system_log import SystemLogSink, install_system_log_sink

#: 保留多少天。海盗一轮半小时、光 `say()` 就 80 个调用点，两周的量级是几十万行，
#: SQLite 与 Postgres 都还轻松；再长就只是在攒没人会翻的历史。
DEFAULT_RETENTION_DAYS = 14


def database_sink(session_factory: sessionmaker[Session], **options: object) -> SystemLogSink:
    """一个写进 `system_log` 表的 sink。

    传的是仓储的 `append`，它**照抛异常**——吞异常是 sink 的边界，见
    `storage/system_log.py::SystemLogRepository.append` 的注释。
    """
    return SystemLogSink(SystemLogRepository(session_factory).append, **options)  # type: ignore[arg-type]


def install_database_system_log(
    session_factory: sessionmaker[Session], **options: object
) -> SystemLogSink | None:
    """装上进程级的日志出口。**建不起来就返回 None，绝不把调用方拦下。**

    这条路径上的失败面比看起来大：URL 写错、psycopg 没装（`uv sync` 少带
    `--extra db` 就会），内网不通。任何一条都不该让实机 runner 或控制台起不来——
    日志入库是**额外的一份**，本机的 print 与 `var/logs/` 仍然是全的。
    """
    try:
        return install_system_log_sink(database_sink(session_factory, **options))
    except Exception:  # noqa: BLE001 - 见函数注释：日志出口不许挡住启动
        return None


def purge_system_log(
    session_factory: sessionmaker[Session],
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
) -> int:
    """删掉超过保留期的日志，返回删了几行。失败返回 0，不抛。

    `retention_days <= 0` 视为「不清理」：把它当成「全删」太危险，一个手滑的
    配置值就能把整张表清空，而这张表正是出事之后唯一能翻的东西。
    """
    if retention_days <= 0:
        return 0
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=retention_days)
    try:
        return SystemLogRepository(session_factory).purge_before(cutoff)
    except Exception:  # noqa: BLE001 - 清理失败不该把控制台的启动拖垮
        return 0


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "database_sink",
    "install_database_system_log",
    "purge_system_log",
]
