"""实机 runner 的日志接线：一句话把 `system_log` 出口装上。

四个 runner（`pirate_loop` / `bot_loop` / `scan_coordinates` / `ranking_scan`）
各自是独立进程，每个都要自己装一次。装在 `main()` 里而不是模块 import 时：
`import` 一个工具模块不该在背地里建数据库连接，测试尤其不该。

## 两条路，不是一条

`say()` / `warn()` 那条走 `record_system_log`，装上出口就够了。但仓里还有
**第二条**：`vision/live_reports.py` 与 `vision/pirate_reports.py` 用的是标准库
`logging`，而 runner 进程从来没调过 `configure_logging()`，`evo_helper` 这棵
logger 一个 handler 都没有。所以在此之前，那几条 `logger.warning` 只被
`logging.lastResort` 丢到 stderr，**一条都进不了库**。

⚠️ 这不是理论问题。PR #165 给「获得资源没读全」加的那条 warning 就在这条路上：
它是「12 格全是 0」与「那一屏根本没读出来」唯一的分界证据，而两者交出去的都是
空元组。跨机排障时（实机在另一台机器上）stderr 与 `var/logs/` 都取不到，等于
这条链路整块失灵也看不见——正是 `record_unrecognised_screen` 那次的同一个坑。
"""

from __future__ import annotations

import logging

from evo_helper.config import Settings
from evo_helper.infrastructure.system_log import SystemLogSink, attach_system_log_handler
from evo_helper.infrastructure.system_log_db import install_database_system_log
from evo_helper.storage.database import create_database_engine, create_session_factory

#: 本机那份 stderr 副本的 handler 名。装第二次时靠它认出来，不叠第二个。
CONSOLE_HANDLER_NAME = "evo-runner-console"

_CONSOLE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def install_runner_system_log(settings: Settings | None = None) -> SystemLogSink | None:
    """把本进程的 `say()` 与标准库日志接到 `system_log` 表上。

    **建不起来就安静地返回 None。** 库在内网另一台机器上，连不上是常态之一，
    而实机 runner 不该因为「日志写不进去」而起不来——本机 cmd 窗口与
    `var/logs/mission-*.log` 那两份仍然是全的。

    ⚠️ **两个 handler 都在出口之前装。** 出口建不起来时 `record_system_log`
    是空操作，装着的 DB handler 跟着空转，无害；反过来把 handler 排在
    `try` 里面，就成了「库一连不上，本机连 stderr 那份也一起没了」。
    """
    attach_console_handler()
    # 标准库 → `system_log` 的桥。控制台进程在 `web.runtime` 里装的是同一个。
    attach_system_log_handler()
    try:
        resolved = settings or Settings()
        engine = create_database_engine(resolved.database_url)
        return install_database_system_log(create_session_factory(engine))
    except Exception:  # noqa: BLE001 - 见函数注释：日志出口不许挡住 runner 启动
        return None


def attach_console_handler(*, level: int = logging.WARNING) -> None:
    """把标准库日志的**本机那一份**接回 stderr。可重复调用，不会叠。

    ⚠️ **少了它就是一次静默的倒退。** 装上 DB handler 的那一刻，
    `logging.lastResort` 就不再兜底了（它只在「一个 handler 都没有」时才发话），
    于是 runner 那台机器的 `var/logs/mission-*.log`（stderr 并进 stdout，见
    `application.mission_supervisor`）会从此少掉那几条 warning——把日志搬进库
    绝不该以本机那份变少为代价，`say()` 那条路一直是双写的。

    级别取 `WARNING` 而不是 INFO：本机那份原先就是 `lastResort` 的 WARNING，
    放到 INFO 等于给 runner 控制台凭空多出一堆从来没有过的行。入库那份仍是
    INFO（`attach_system_log_handler` 的默认值），两份的取舍本来就不同——
    库是拿来事后翻的，控制台是当场看的。
    """
    logger = logging.getLogger("evo_helper")
    if any(handler.name == CONSOLE_HANDLER_NAME for handler in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.name = CONSOLE_HANDLER_NAME
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    logger.addHandler(handler)


__all__ = ["CONSOLE_HANDLER_NAME", "attach_console_handler", "install_runner_system_log"]
