"""Runnable persistent EVO-Helper Web service.

默认绑 `0.0.0.0:8770`，局域网内的其他设备可直接访问；不设访问口令
（用户确认），因此只适合可信内网。要退回本机独占：`EVO_HELPER_HOST=127.0.0.1`。
"""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from alembic.config import Config
from fastapi import FastAPI

from alembic import command
from evo_helper.application.ai_targeting import DEFAULT_AI_RETENTION_DAYS
from evo_helper.config import Settings
from evo_helper.infrastructure.system_log import attach_system_log_handler
from evo_helper.infrastructure.system_log_db import install_database_system_log, purge_system_log
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.report_screenshots import purge_report_screenshots
from evo_helper.storage.repository import SqlAlchemyRepository

from .app import create_persistent_app


def create_runtime_app(
    settings: Settings | None = None, *, local_token: str | None = None
) -> FastAPI:
    """建出真正的控制台 app。**这里不跑迁移。**

    ⚠️ **迁移只在 `main()` 里跑，也就是只在 bat 真正启动服务的那条路上。**

    这个函数以前顺手跑了 `alembic upgrade head`，于是**任何人只要构造一次 app
    就会静默升级它连到的那个库**——写个脚本渲染页面、跑个诊断、在 REPL 里看一眼，
    都算。2026-08-17 我就是这么在**生产库**上跑掉了七条迁移：当时只是想渲染
    `/system-log` 看一个翻页 bug，全程没有任何提示。

    这件事的严重性不在版本号对不对，而在**schema 变更回退不了**：代码能回滚，
    已经改过的表回不去。而那次操作既没有备份、也没有经过用户同意。

    用户口径（2026-08-17）：迁移由生产自己升——「开发过程中不要影响生产环境，
    但是生产环境重新执行 bat 后会执行数据库表结构最新修改」。`main()` 就是
    「重新执行 bat」那条路，所以判据放在那里才对得上这句话。

    把闸门挪到 `main()`，是把「靠人记住别用错函数」换成「用错也不出事」。
    """
    actual_settings = settings or Settings()
    engine = create_database_engine(actual_settings.database_url)
    session_factory = create_session_factory(engine)
    # 系统日志：出口 + 标准库桥 + 保留期清理，三件都只在**真实进程**里做。
    #
    # ⚠️ 装在这里而不是 `create_persistent_app` 里：那个工厂是测试建 app 的入口，
    # 在它里面起后台线程、连库、改全局 logger，会让每一个 web 测试都带上一份
    # 进程级副作用。
    install_database_system_log(session_factory)
    # 控制台进程**从来没调过** `configure_logging()`，所以 `evo_helper` 这棵 logger
    # 一个 handler 都没有：`mission_scheduler` 的两条 `_LOGGER.info`、
    # `vision.live_reports` 的 info 此前哪儿都到不了，连本机控制台都没有。
    attach_system_log_handler()
    # 保留期清理挂在启动上：这是本进程唯一一个「每次开机跑一次」的现成时机，
    # 而这张表按设计只增不改，攒着不清迟早把库撑大。
    purge_system_log(session_factory, retention_days=actual_settings.system_log_retention_days)
    # 战报截图照同一条路子清：也是「只增不改、攒着会撑大库」的旁路数据，
    # 也没有别的「每次开机跑一次」的时机。两者的保留期各自配（图贵得多）。
    purge_report_screenshots(
        session_factory, retention_days=actual_settings.report_screenshot_retention_days
    )
    # AI 选靶（影子）记录照同一条路子清。保留天数旋钮在攻击配置页上
    # （`military_attack_config.ai_retention_days`，NULL = 代码默认 90 天），
    # 不在 Settings——它和别的攻击旋钮住在同一张表。
    _purge_ai_target_decisions(session_factory)
    # 旧版把每个 `bot_<g>_<s>_<position>` 都纳入候选；固定海盗位 1--4
    # 因而被错误固化。保留原始扫描/榜单记录，只撤销派遣候选资格。
    SqlAlchemyRepository(session_factory).clear_pirate_position_bot_candidates()
    app = create_persistent_app(session_factory, settings=actual_settings, local_token=local_token)
    app.state.database_engine = engine
    return app


def _purge_ai_target_decisions(session_factory: Any) -> int:
    """清掉早于保留期的 AI 选靶影子记录。保留期从 `military_attack_config` 读。

    0 或负数 = 不清理（同 `system_log_retention_days` 的口径，不是「全删」）。
    表没初始化 / 配置行不存在时直接返回 0——这是旁路数据，清不到不影响任何事。
    """
    repository = SqlAlchemyRepository(session_factory)
    try:
        row = repository.military_attack_config()
    except ValueError:
        return 0
    days = row.ai_retention_days
    if days is None:
        days = DEFAULT_AI_RETENTION_DAYS
    if days <= 0:
        return 0
    cutoff = datetime.now(UTC) - timedelta(days=days)
    try:
        return repository.purge_ai_target_decisions(cutoff)
    except Exception:  # noqa: BLE001 - 保留期清理失败不影响服务启动
        return 0


def lan_address() -> str | None:
    """本机在局域网上的地址，拿不到就返回 None。

    用连 UDP 的办法问路由表要出口网卡地址——`gethostbyname(gethostname())`
    在多网卡（VPN、WSL、虚拟机桥接）的机器上经常给出一个连不通的地址，
    而这个地址是要打印给用户拿手机去输的，给错还不如不给。
    不发包，所以不需要外网可达。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def announce(settings: Settings) -> list[str]:
    """启动横幅。地址不打印出来，用户就得自己去 ipconfig 里翻。"""
    lines = [f"控制台监听 {settings.host}:{settings.port}"]
    lines.append(f"  本机   http://127.0.0.1:{settings.port}/")
    if settings.lan_exposed:
        address = lan_address()
        if address:
            lines.append(f"  局域网 http://{address}:{settings.port}/")
        # 这行是打到 Windows 控制台上的，默认代码页 cp936。`⚠` 之类的符号
        # 不在 GBK 里，print 会抛 UnicodeEncodeError——启动横幅把服务本身弄崩。
        lines.append("  [警告] 未设访问口令：同网段内任何设备都能读取情报并启停任务")
    return lines


def main() -> int:
    """Start the local service on the configured interface.

    ⚠️ **迁移在这里跑，而且只在这里。** 这是 bat 启动服务那条路——用户口径
    （2026-08-17）：「生产环境重新执行 bat 后会执行数据库表结构最新修改」。
    别把它挪回 `create_runtime_app`：那样任何构造一次 app 的代码都会静默
    升级生产库，理由见那个函数的说明。
    """
    settings = Settings()
    for line in announce(settings):
        print(line)
    _upgrade_database(settings.database_url)
    uvicorn.run(create_runtime_app(settings), host=settings.host, port=settings.port)
    return 0


def _upgrade_database(database_url: str) -> None:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    command.upgrade(config, "head")
