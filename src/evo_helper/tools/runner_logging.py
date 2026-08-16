"""实机 runner 的日志接线：一句话把 `system_log` 出口装上。

四个 runner（`pirate_loop` / `bot_loop` / `scan_coordinates` / `ranking_scan`）
各自是独立进程，每个都要自己装一次。装在 `main()` 里而不是模块 import 时：
`import` 一个工具模块不该在背地里建数据库连接，测试尤其不该。
"""

from __future__ import annotations

from evo_helper.config import Settings
from evo_helper.infrastructure.system_log import SystemLogSink
from evo_helper.infrastructure.system_log_db import install_database_system_log
from evo_helper.storage.database import create_database_engine, create_session_factory


def install_runner_system_log(settings: Settings | None = None) -> SystemLogSink | None:
    """把本进程的 `say()` 与标准库日志接到 `system_log` 表上。

    **建不起来就安静地返回 None。** 库在内网另一台机器上，连不上是常态之一，
    而实机 runner 不该因为「日志写不进去」而起不来——本机 cmd 窗口与
    `var/logs/mission-*.log` 那两份仍然是全的。
    """
    try:
        resolved = settings or Settings()
        engine = create_database_engine(resolved.database_url)
        return install_database_system_log(create_session_factory(engine))
    except Exception:  # noqa: BLE001 - 见函数注释：日志出口不许挡住 runner 启动
        return None


__all__ = ["install_runner_system_log"]
