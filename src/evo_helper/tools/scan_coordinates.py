"""按优先级顺序逐坐标扫描，把 bot 位置写进数据库。

这是把 scratchpad 里的临时扫描脚本正式化的版本，接上了四件事：

- `domain.scan_plan` —— 已排好的优先级顺序（2:001–200 优先，9 系补末尾）+ 跳过 1–4 位
- `game.game_window.ensure_game_window` —— 窗口没了自己拉起来
- `game.session_keeper.SessionKeeper` —— 每 10 分钟巡检会话，掉线自己接回去
- `run_instances` 的持久化游标 —— 中断后从下一个坐标继续，不重扫也不跳过

**只读**：全程只有导航点击（输入框 / OK），没有任何派遣、领奖、删信路径；
`HumanInput` 会拒绝任何看起来像动作的标签。

    python -m evo_helper.tools.scan_coordinates --limit 200
    python -m evo_helper.tools.scan_coordinates --status
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import CoordinateScan
from evo_helper.domain.scan_bounds import ScanBounds
from evo_helper.domain.scan_plan import iter_scan_coordinates, planned_segments, total_coordinates
from evo_helper.domain.scheduler import EXIT_ENVIRONMENT_BUSY, exit_code_for_environment_fault
from evo_helper.game.game_window import ForegroundUnavailable
from evo_helper.game.overlay import dismiss_overlays, look_at_close_button
from evo_helper.game.ranking_ui import WHEEL_DELTA
from evo_helper.game.system_navigator import NAV_LABEL_ROI, SystemNavigator, crop_reader
from evo_helper.infrastructure.system_log import record_system_log
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.tools.runner_logging import install_runner_system_log
from evo_helper.vision.scan_reading import (
    COORD_WHITELIST,
    PlanetPanel,
    looks_like_mangled_bot,
    read_panel_confirming,
)

#: 这次全宇宙扫描用的计划名与幂等键。重跑同一个键就是续扫，不会新开一轮。
PLAN_NAME = "全宇宙优先级扫描"
RUN_KEY = "priority-scan-0001"

PRESET_NAME = "探路"
PRESET_SIGNATURE = "轻型战斗机:1"

#: 连续多少个坐标读不出坐标就停。单个读失败是噪声，连着失败说明画面已经不是面板了
#: （弹窗、维护公告、掉线），继续点下去就是在认不出的画面上乱点。
MAX_CONSECUTIVE_FAILURES = 5

#: 同一个坐标核对不过时重读几次。相邻重复数字会粘连（`2:2:11` 读成 `[2:2:1]`），
#: 只读一次就放弃会把这个坐标永久漏掉。
READ_ATTEMPTS = 3

#: 本次跑下来始终核对不过的坐标记在这里，供 `--rescan-missing` 或人工复核。
GAP_LOG = Path("var/logs/scan-gaps.txt")

#: **每个恒星系恰好一个 bot**（用户确认的游戏规则；实测 111/111 相符）。
#:
#: 据此在找到某系的 bot 之后跳过该系剩余行星位。bot 位号分布均匀，期望扫 8.5 位而不是 16 位，
#: 全宇宙耗时约减半（144 小时 → 约 76 小时）。
#:
#: 「已扫完」的判据因此**不是**「16 个位都入库」，而是「找到了 bot 或 16 个位都入库」。
#: 这条判据必须只有一份：扫描主循环和 `--rescan-missing` 的补缺口都用 `systems_with_bot()`，
#: 否则补缺口会把主循环故意跳过的坐标当成缺口，一遍遍重扫。
#:
#: `--scan-full-systems` 可关掉它，把这条假设变回可撤销的。
ONE_BOT_PER_SYSTEM = True


def origin() -> Coordinate:
    """出发星球。默认值在 `domain.missions.ORIGIN`，换账号用 `EVO_HELPER_ORIGIN` 覆盖。

    主星原先在三个文件各写了一遍，现在解析只有这一份，另外两条链路借用它
    （`pirate_loop` 直接 import，调度器由 `application` 层注入）。

    做成函数而不是模块常量：常量在 import 那一刻就把值定死了，之后 `.env` 或
    环境变量再改都不生效——而「配置改了却没生效」完全不报错，只是舰队从上一个
    账号的主星出发，飞行时间和战报匹配跟着一起错。
    """
    return Settings().origin_coordinate


def tesseract_path() -> Path:
    """Tesseract 可执行文件。同上，函数而非常量，理由见 `origin`。"""
    return Path(Settings().tesseract_path)


# -- 输出 ---------------------------------------------------------------------


def say(message: str) -> None:
    """打一行带时刻的日志。**编码安全：这一句永远不许把进程弄死。**

    实机事故（2026-08-10 首次真派遣）：OCR 从简报上读出来的字里带了个 `™`，
    而 stdout 被调度器重定向到文件、Python 用的是本地代码页 GBK，`print` 当场
    抛 `UnicodeEncodeError`。要命的是这一句正在 `_dump_frame` 的**诊断路径**上：
    本来是「简报认不出，安全地不派这一发」，结果变成整个 runner 崩在半路、
    游戏被留在一个开着的面板上，接着填空隙的扫描也认不出画面、连挂三次被自动停用。
    一个可恢复的判定失败，就这样级联成了整条链路停摆。

    OCR 的输出本来就什么字符都可能有，所以这里按当前流的编码兜一层：
    编不出来的字换成替代符，宁可丢一个字符也不能丢一个进程。

    **同一行还会进 `system_log` 表（双写）。** 实机跑在一台机器上、人常在另一台
    机器上看控制台，本机 cmd 窗口与 `var/logs/mission-*.log` 换台机器就看不见了。
    入库走的是有界队列 + 后台线程（`infrastructure.system_log`），调用方不等
    网络往返；没装出口时那一句是空操作。**它排在 `print` 之后**：控制台那份是
    保底的一份，不该因为日志出口出问题而少打一行。
    """
    line = f"{datetime.now().strftime('%H:%M:%S')} {message}"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, flush=True)
    record_system_log("INFO", _caller_source(), message)


def warn(message: str) -> None:
    """同 `say()`，但落进 `system_log` 时级别是 `WARNING`。

    有这个出口是因为控制台的日志页可以**按级别筛**：只用 `say()` 的话，一条
    「盲拖快拖过头了」会和一轮几千行 INFO 混在一起，等于没报。控制台那份仍然
    照常打印——两份输出的内容一致，只有级别不同。
    """
    line = f"{datetime.now().strftime('%H:%M:%S')} {message}"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = line.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe, flush=True)
    record_system_log("WARNING", _caller_source(), message)


def _caller_source() -> str:
    """`say()` 的调用方模块名，去掉 `evo_helper.` 前缀。

    从调用栈里取而不是让每个调用点自己传：`say` 有 136 个调用点（pirate_loop 80、
    scan_coordinates 33、bot_loop 14、backfill_reports 等 9），改签名等于全改一遍，
    而漏掉任何一个都会让那一条日志的 `source` 说谎。

    取不到就回落到本模块名——`sys._getframe` 是 CPython 的实现细节，而
    「日志少一个准确的来源」远好过「日志出口把 runner 弄死」。
    """
    try:
        name = sys._getframe(2).f_globals.get("__name__", __name__)
    except (AttributeError, ValueError):  # pragma: no cover - 非 CPython 才走到
        name = __name__
    return str(name).removeprefix("evo_helper.")[:64]


def make_console_encoding_safe() -> None:
    """把 `say()` 那层保护推广到**整个进程的输出**，命令行入口第一句就调它。

    `say()` 只保护自己那一行，而进程里还有别的地方会往 stdout / stderr 写字，
    最要命的是 **argparse**：`--help` 与「参数写错了」都直接 `file.write()`，
    绕开 `say()`。本仓的帮助文本里有 `⚠️`（U+26A0），而 Windows 控制台是 GBK——

        UnicodeEncodeError: 'gbk' codec can't encode character '\\u26a0'

    `--help` 打不出来只是难受；**参数写错那一条要命**：argparse 本来要告诉你
    错在哪，结果那句话自己崩了，你看到的是一段和真实错误毫无关系的编码栈。

    这是 2026-08-10 那次事故（`say()` 的注释里记着：OCR 读出个 `™` 把 runner
    崩在诊断路径上）的**同一个教训、另一条出口**。当时只补了 `say()` 这一处。

    做法是给流本身挂上 `errors="replace"`，**不改编码**：改成 UTF-8 会让所有中文
    在 GBK 控制台上变成乱码，那是拿一个小毛病换一个大毛病。编不出来的字符换成
    替代符——宁可丢一个字符，不能丢一个进程。

    流不支持 `reconfigure`（被重定向成别的对象、或者根本没有 stdout）时什么都不做：
    这个函数自己绝不能成为新的崩溃点。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):  # pragma: no cover - 流已关闭或不可重配
            continue


# -- 计划与游标 ----------------------------------------------------------------


def configured_priority_planets(session_factory: Any) -> tuple[Coordinate, ...]:
    """按星球列表顺序取扫描优先中心；没有配置时退回兼容的旧计划。"""
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with session_factory() as session:
        rows = session.scalars(
            select(orm.AttackPlanetRow).order_by(orm.AttackPlanetRow.sort_index)
        ).all()
    return tuple(Coordinate(row.galaxy, row.system, row.position) for row in rows)


def _range_specs(
    priority_planets: tuple[Coordinate, ...], home: Coordinate
) -> tuple[tuple[int, int, int, int, int, int, int, int, int, int], ...]:
    """计划范围的可比较快照；范围变化时旧游标必须重新从优先区开始。"""
    return tuple(
        (
            start.galaxy,
            start.system,
            start.position,
            end.galaxy,
            end.system,
            end.position,
            home.galaxy,
            home.system,
            home.position,
            index,
        )
        for index, (_segment, start, end) in enumerate(
            planned_segments(priority_planets=priority_planets)
        )
    )


def _stored_range_specs(
    ranges: list[Any],
) -> tuple[tuple[int, int, int, int, int, int, int, int, int, int], ...]:
    return tuple(
        (
            row.start_galaxy,
            row.start_system,
            row.start_position,
            row.end_galaxy,
            row.end_system,
            row.end_position,
            row.origin_galaxy,
            row.origin_system,
            row.origin_position,
            row.priority,
        )
        for row in ranges
    )


def ensure_run(
    session_factory: Any, *, priority_planets: tuple[Coordinate, ...] | None = None
) -> tuple[UUID, Coordinate | None]:
    """找到（或建好）扫描运行实例，并让持久化计划跟随星球列表。

    旧版本的计划固定从 2 系开始。发现范围快照不一致时，范围和游标一起更新，
    已确认坐标仍由 ``BotTargetRow`` 去重，所以不会因为重排而重复扫描。
    """
    from sqlalchemy import delete, select

    from evo_helper.storage import models as orm

    now = datetime.now(UTC)
    # 计划表要求出发星球非空。扫描本身用不到它——扫描不派遣。
    home = origin()
    planets = (
        priority_planets
        if priority_planets is not None
        else configured_priority_planets(session_factory)
    )
    wanted_ranges = _range_specs(planets, home)
    with session_factory() as session:
        plan = session.scalar(select(orm.ScanPlan).where(orm.ScanPlan.name == PLAN_NAME))
        if plan is None:
            plan = orm.ScanPlan(
                name=PLAN_NAME,
                enabled=True,
                time_window_start="00:00",
                time_window_end="23:59",
                created_at_utc=now,
                updated_at_utc=now,
            )
            session.add(plan)
            session.flush()
        existing_ranges = session.scalars(
            select(orm.ScanRangeRow)
            .where(orm.ScanRangeRow.plan_id == plan.id)
            .order_by(orm.ScanRangeRow.priority, orm.ScanRangeRow.id)
        ).all()
        ranges_changed = _stored_range_specs(existing_ranges) != wanted_ranges
        if ranges_changed:
            session.execute(delete(orm.ScanRangeRow).where(orm.ScanRangeRow.plan_id == plan.id))
            for spec in wanted_ranges:
                (
                    start_galaxy,
                    start_system,
                    start_position,
                    end_galaxy,
                    end_system,
                    end_position,
                    origin_galaxy,
                    origin_system,
                    origin_position,
                    priority,
                ) = spec
                session.add(
                    orm.ScanRangeRow(
                        plan_id=plan.id,
                        start_galaxy=start_galaxy,
                        start_system=start_system,
                        start_position=start_position,
                        end_galaxy=end_galaxy,
                        end_system=end_system,
                        end_position=end_position,
                        origin_galaxy=origin_galaxy,
                        origin_system=origin_system,
                        origin_position=origin_position,
                        fleet_preset_name=PRESET_NAME,
                        fleet_preset_signature=PRESET_SIGNATURE,
                        priority=priority,
                    )
                )
            plan.updated_at_utc = now

        run = session.scalar(
            select(orm.RunInstance).where(orm.RunInstance.idempotency_key == RUN_KEY)
        )
        if run is not None:
            if ranges_changed:
                # 老游标指向旧顺序；保留已扫记录，重新从新的局部优先区取数。
                run.cursor_galaxy = None
                run.cursor_system = None
                run.cursor_position = None
                run.pending_galaxy = None
                run.pending_system = None
                run.pending_position = None
            session.commit()
            return run.id, _cursor_of(run)

        run = orm.RunInstance(
            plan_id=plan.id,
            idempotency_key=RUN_KEY,
            state="SCANNING",
            started_at_utc=now,
            created_at_utc=now,
        )
        session.add(run)
        session.commit()
        return run.id, None


def _cursor_of(run: Any) -> Coordinate | None:
    if run.cursor_galaxy is None:
        return None
    return Coordinate(run.cursor_galaxy, run.cursor_system, run.cursor_position)


def save_cursor(session_factory: Any, run_id: UUID, coordinate: Coordinate) -> None:
    """游标只在一个坐标**读完并落库之后**才前进，所以中断最多重扫一个坐标。"""
    from evo_helper.storage import models as orm

    with session_factory() as session:
        run = session.get(orm.RunInstance, run_id)
        if run is None:  # pragma: no cover - 运行实例刚建好，不可能不在
            raise RuntimeError(f"运行实例不见了: {run_id}")
        run.cursor_galaxy = coordinate.galaxy
        run.cursor_system = coordinate.system
        run.cursor_position = coordinate.position
        session.commit()


def already_scanned(session_factory: Any) -> set[tuple[int, int, int]]:
    """先前已经确认过的坐标，续扫时跳过。

    早先那 71 条是另一个运行实例写的，按运行实例过滤会把它们重扫一遍。
    """
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with session_factory() as session:
        rows = session.execute(
            select(orm.BotTargetRow.galaxy, orm.BotTargetRow.system, orm.BotTargetRow.position)
        ).all()
    return {(row[0], row[1], row[2]) for row in rows}


# -- OCR ----------------------------------------------------------------------


def record_gap(coordinate: Coordinate, raw_text: str) -> None:
    """把始终核对不过的坐标追加到缺口清单。"""
    GAP_LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    line = f"{stamp}\t{coordinate.galaxy}:{coordinate.system}:{coordinate.position}\t{raw_text!r}\n"
    with GAP_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line)


def systems_with_bot(session_factory: Any) -> set[tuple[int, int]]:
    """已经找到 bot 的恒星系。每系只有一个，所以这些系不用再扫了。"""
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with session_factory() as session:
        rows = session.execute(
            select(orm.BotTargetRow.galaxy, orm.BotTargetRow.system).where(
                orm.BotTargetRow.is_bot.is_(True)
            )
        ).all()
    return {(row[0], row[1]) for row in rows}


def missing_from_plan(
    session_factory: Any,
    *,
    upto: Coordinate | None,
    one_bot_per_system: bool = ONE_BOT_PER_SYSTEM,
    priority_planets: tuple[Coordinate, ...] = (),
) -> Iterator[Coordinate]:
    """计划里走到过、但库里没有的坐标。

    核对失败的坐标不会入库，游标却会被后面成功的坐标带过去。缺口清单是给人看的，
    这个函数是给机器补的——它直接对着「计划 vs 已入库」求差，不依赖任何日志。

    **已经找到 bot 的恒星系整个跳过**：那些位是主循环按规则故意不扫的，不是缺口。
    两处用同一个判据，否则补缺口会没完没了地重扫它们。
    """
    if upto is None:
        return
    done = already_scanned(session_factory)
    found = systems_with_bot(session_factory) if one_bot_per_system else set()
    for coordinate in iter_scan_coordinates(priority_planets=priority_planets):
        if (coordinate.galaxy, coordinate.system) not in found and (
            coordinate.galaxy,
            coordinate.system,
            coordinate.position,
        ) not in done:
            yield coordinate
        if coordinate == upto:
            return


def suspicious_names(session_factory: Any) -> list[Coordinate]:
    """库里那些「名字像 bot 但没判成 bot」的坐标。

    `bot_2_9_5` 读成 `botleao.-`，前缀糊了，于是那颗星球作为普通空位入了库——
    而它正是要找的东西。这类失败不报错，只能反查。
    """
    from sqlalchemy import select

    from evo_helper.storage import models as orm

    with session_factory() as session:
        rows = session.execute(
            select(
                orm.BotTargetRow.galaxy,
                orm.BotTargetRow.system,
                orm.BotTargetRow.position,
                orm.BotTargetRow.latest_owner_name,
            ).where(orm.BotTargetRow.is_bot.is_(False))
        ).all()
    return [Coordinate(row[0], row[1], row[2]) for row in rows if looks_like_mangled_bot(row[3])]


#: 识别中文要用的语言包。缺了它整条链路会**安静地**跑一整夜。
CHINESE_LANGUAGE = "chi_sim"


def installed_ocr_languages(executable: Path) -> frozenset[str]:
    """问 Tesseract 装了哪些语言包。问不出来就返回空集，由调用方决定怎么办。"""
    import subprocess

    try:
        completed = subprocess.run(  # noqa: S603 - 路径来自本机配置，不是外部输入
            [str(executable), "--list-langs"], capture_output=True, text=True, timeout=20
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    # 第一行是「List of available languages ...」，之后每行一个语言名。
    lines = (completed.stdout or completed.stderr or "").splitlines()
    return frozenset(line.strip() for line in lines[1:] if line.strip())


def require_chinese_ocr() -> None:
    """没装中文语言包就**当场停下**，不要带着这个残疾开工。

    ⚠️ **2026-08-17 实机：这一条缺席，代价是一整夜。** 另一台机器的 Tesseract 只装了
    `eng` / `osd`，于是画面上每一处中文都读成拉丁噪声：

        导航条「行星 舰队 太空舱 商店 联盟」  →  '72 MB = oKSAtC(itéiaG EA'
        入口页「进入」                        →  ''

    而 `IN_GAME_MARKERS` 全是中文，于是**永远判不出「在游戏里」**，每一轮都一路掉到
    最后去试 START、判 `unrecognised screen`、关窗重开 Chrome、再认不出——如此循环
    一小时，环境故障计数打到 6/6 上限，而**全程没有任何一条日志说得出原因**。

    判据的分辨力就在这里：英文那几屏它读得像模像样（`'member hitting you did I ?'`），
    中文全是噪声——两者一对比就知道是语言包，不是坐标、不是窗口、不是掉线。

    所以宁可开工即停：一条明确的错误好过一夜看不出所以然的空转。
    """
    executable = tesseract_path()
    languages = installed_ocr_languages(executable)
    if not languages or CHINESE_LANGUAGE in languages:
        # 问不出来时放行：`--list-langs` 的输出格式不是契约，认不出就别拦路。
        return
    message = (
        f"Tesseract 没装中文语言包（{CHINESE_LANGUAGE}）：{executable} 只有 "
        f"{'/'.join(sorted(languages))}。画面上每一处中文都会读成噪声，"
        f"链路会认不出「在游戏里」而空转。把 {CHINESE_LANGUAGE}.traineddata "
        f"放进 tessdata 目录后重试。"
    )
    record_system_log("ERROR", "tools.scan_coordinates", message)
    raise RuntimeError(message)


#: `tight` 补的那圈黑边至少这么宽（放大后的像素）。太窄 Tesseract 会把字当成
#: 贴边噪声，太宽就退回「小字浮在大片空白里」那个病。
INK_MARGIN_MIN_PX = 8


def _crop_to_ink(binary: Any, image_module: Any) -> Any:
    """二值图裁到白色墨迹的外框，再补一圈黑边。空图原样交回。

    ## 这一步治的是什么

    导航栏值框是 135×33 的一大片黑，数字只占中间一小块。`--psm 7`（单行）在这种
    「小字浮在大片空白里」的图上会**漏字**——实测二值化后的 `52` 清晰可辨，
    Tesseract 却只读出 `5`；生产上同一个病表现为 `277` 读成 `77`（`system_navigator
    .NAV_VALUE_RECIPES` 里记着那 28 次 0% 成功率）。把字撑满画面之后就基本消失。

    边距按字高取（`ink.height // 2`），不是定值：字大边距也要大，否则比例又失衡。
    """
    box = binary.getbbox()
    if box is None:
        return binary
    ink = binary.crop(box)
    margin = max(INK_MARGIN_MIN_PX, ink.height // 2)
    canvas = image_module.new("L", (ink.width + margin * 2, ink.height + margin * 2), 0)
    canvas.paste(ink, (margin, margin))
    return canvas


def make_ocr() -> Any:
    import pytesseract
    from PIL import Image

    require_chinese_ocr()
    pytesseract.pytesseract.tesseract_cmd = str(tesseract_path())

    filters = {"lanczos": Image.Resampling.LANCZOS, "nearest": Image.Resampling.NEAREST}

    def ocr(
        crop: Any,
        *,
        digits: bool,
        upscale: int,
        resample: str = "lanczos",
        threshold: int | None = None,
        tight: bool = False,
    ) -> str:
        grey = crop.convert("L")
        # 最近邻放大保住相邻 1 之间那道缝；LANCZOS 会把它插值糊掉。见 COORD_RECIPES。
        grey = grey.resize((grey.width * upscale, grey.height * upscale), filters[resample])
        if threshold is not None:
            # START 是半透明的大字压在星空上，不二值化读不出来。
            grey = grey.point(lambda value: 255 if value > threshold else 0)
        if tight and threshold is not None:
            grey = _crop_to_ink(grey, Image)
        if digits:
            # 混合语言下开头的 2 会被读成 e；数字白名单能解决。
            config = f"--psm 7 -c tessedit_char_whitelist={COORD_WHITELIST}"
            return str(pytesseract.image_to_string(grey, lang="eng", config=config)).strip()
        return str(pytesseract.image_to_string(grey, lang="chi_sim+eng", config="--psm 7")).strip()

    return ocr


# -- 驱动 ----------------------------------------------------------------------


class LiveDriver:
    """真实鼠标 + 窗口截图。窗口不见了就自己拉起来并重新定位。"""

    def __init__(self, *, allow_actions: bool = False) -> None:
        """``allow_actions`` 默认关：扫描器只导航，不该有能力把舰队送出去。

        攻击链路要点「攻击」和「出发！」，必须在构造时显式打开——
        **开关只有这一处**，翻一眼构造点就知道哪个进程有动作能力。
        """
        from evo_helper.game.human_input import HumanInput, load_pyautogui

        self._gui = load_pyautogui()
        self._human = HumanInput(self._gui, allow_actions=allow_actions)

    def window(self) -> Any:
        """返回一个**可以往上点**的游戏窗口。

        最小化的窗口 `find_game_window()` 照样找得到，但它的 rect 是 (-32000, -32000)，
        照着算出来的点击坐标会落到屏幕角落——实测就是这样触发了 `pyautogui` 的急停。
        急停兜住了，可依赖急停等于把「点到桌面上」当成正常路径。这里先还原再用。
        """
        import win32gui

        from evo_helper.game.game_window import ensure_game_window, find_game_window

        found = find_game_window()
        if found is None or win32gui.IsIconic(found.handle):
            return ensure_game_window()
        return found

    def _raise_to_front(self, handle: int) -> None:
        """试一次抢前台。被系统拒绝是常事，所以吞掉异常交给外层重试。

        Windows 只允许「当前拥有前台的那个线程」换前台。先把自己的输入队列
        挂到前台线程上，`SetForegroundWindow` 才有资格成功——直接调用会被拒，
        而且 `GetLastError` 返回 0，异常里什么信息都没有。
        """
        import win32api
        import win32con
        import win32gui
        import win32process

        win32gui.ShowWindow(handle, win32con.SW_SHOW)
        attached = False
        target, _ = win32process.GetWindowThreadProcessId(handle)
        current = win32api.GetCurrentThreadId()
        try:
            if target != current:
                attached = bool(win32process.AttachThreadInput(current, target, True))
            win32gui.SetForegroundWindow(handle)
        except Exception:  # noqa: BLE001 - 抢前台被拒绝不是错误，是常态
            pass
        finally:
            if attached:
                try:
                    win32process.AttachThreadInput(current, target, False)
                except Exception:  # noqa: BLE001 - 解绑失败也不该中断扫描
                    pass

    def focus(self, *, attempts: int = 5) -> None:
        """把游戏窗口提到前台。

        **必须真的到前台**：别的窗口盖在上面时点击会落到那个窗口上，而截图走
        `PrintWindow` 照样是游戏画面——于是看起来像「游戏没响应」，实际是点错了地方。

        抢前台会被系统间歇性拒绝（用户正在别的窗口打字时尤其如此），这是常态不是故障，
        所以退避重试；每次都**回读** `GetForegroundWindow` 核实，不信返回值。
        """
        import win32gui

        handle = self.window().handle
        for attempt in range(attempts):
            if win32gui.GetForegroundWindow() == handle:
                return
            self._raise_to_front(handle)
            time.sleep(0.3 * (attempt + 1))
        if win32gui.GetForegroundWindow() != handle:
            # ⚠️ **必须是 `ForegroundUnavailable` 而不是裸 `RuntimeError`。**
            # 各 runner 的 `main()` 靠这个类型把本轮按 `EXIT_ENVIRONMENT_BUSY`
            # 收场（`run_with_foreground_guard`）；换回裸 `RuntimeError` 就退回到
            # 「抛穿 main、退出码 1、计进连续失败」，理由整段写在那个异常类上。
            raise ForegroundUnavailable(
                "游戏窗口抢不到前台（多半是用户正在用别的窗口）；停止而不是把点击打到别人窗口上"
            )

    def origin(self) -> tuple[int, int]:
        from evo_helper.vision.optional.window_capture import client_box

        box = client_box(self.window())
        return box[0], box[1]

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.focus()
        origin_x, origin_y = self.origin()
        self._human.click(origin_x + x, origin_y + y, label=label)

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str = "") -> None:
        """面板内拖动。预设栏一屏只放得下两个预设，其余的要横向拖出来。"""
        self.focus()
        origin_x, origin_y = self.origin()
        self._human.drag(
            origin_x + from_x,
            origin_y + from_y,
            origin_x + to_x,
            origin_y + to_y,
            label=label,
        )

    def type_number(self, value: int) -> None:
        import random

        self._gui.hotkey("ctrl", "a")
        time.sleep(random.uniform(0.15, 0.35))
        for char in str(value):
            self._gui.write(char)
            time.sleep(random.uniform(0.06, 0.18))
        time.sleep(random.uniform(0.2, 0.4))

    def capture(self) -> Any:
        from evo_helper.vision.optional.window_capture import capture_window

        return capture_window(self.window())

    def wait(self, seconds: float) -> None:
        time.sleep(seconds)


class SlowDragDriver:
    """把 `LiveDriver` 包成 `game.ranking_nav.RankingDriver` 要的那个操作面。

    多出来的是分步慢拖的三个原语 `press` / `move_to` / `release`，外加盲滚要的
    `wheel_notch`。`LiveDriver.drag` 是一步式的 `dragTo`，游戏面板会把它当成点击
    （`pirate_loop.slow_drag` 的注释里记着这条实测），而**分步这件事必须发生在
    `game` 层**——否则 `game.ranking_nav` 就得反过来 import `tools`。
    所以分步的循环在那边，这里只提供原语。同 `pirate_loop._PlanetListDriver`。

    ⚠️ **这一层是有状态的**，而 `LiveDriver` 的其余部分不是。「手指正按着」这个
    状态就是它单独存在、而不是把三个方法加到 `LiveDriver` 上的理由：按着不放的
    鼠标是能弄坏用户桌面的东西，把它关在一个用完就扔的小对象里，比让每一条链路
    共用的驱动多一个模式安全。
    """

    def __init__(self, driver: LiveDriver) -> None:
        self._driver = driver
        #: 按下时的窗口原点，同时兼作「手指按着没有」。整趟拖动共用它——
        #: 见 `move_to`。
        self._origin: tuple[int, int] | None = None

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self._driver.click(x, y, label=label)

    def wait(self, seconds: float) -> None:
        self._driver.wait(seconds)

    def press(self, x: int, y: int, *, label: str = "") -> None:
        """抢前台、取原点、移到位、按下。**顺序是有讲究的。**

        先移到位再按下：反过来的话，按下的那一瞬间鼠标还停在上一次的落点上，
        那一下就成了在别的东西上按下并拖走。

        `_origin` 在碰鼠标**之前**就记上，宁可记早了：`moveTo` 抛出来
        （pyautogui 的急停就是从这里抛的）时按键状态是不确定的，而多松一次
        完全无害、漏松一次是把按着的鼠标交还给用户。
        """
        import random

        del label  # 慢拖是分步的，`HumanInput` 那条带标签的路径走不通。
        self._driver.focus()
        origin_x, origin_y = self._driver.origin()
        self._origin = (origin_x, origin_y)
        gui = self._driver._gui  # noqa: SLF001 - 分步控制，`HumanInput` 只有一步式 drag
        gui.moveTo(origin_x + x, origin_y + y, random.uniform(0.2, 0.4))
        gui.mouseDown()

    def move_to(self, x: int, y: int) -> None:
        """拖动途中的一步。**用按下时那个原点，不重新取。**

        `LiveDriver.origin()` 走 `client_box(self.window())`，而 `window()` 会在
        窗口不见时把游戏重新拉起来——这个调用不便宜，而且有副作用。更要紧的是：
        每一步重取一次，窗口只要在拖动途中动了一下，后半程就换了一套参照系，
        这一拖会从中间开始拐弯。

        ⚠️ 这里**不抢前台**。`focus()` 抢不到会退避重试最多 4.5 秒、还会抛异常，
        而这两件事都发生在手指按着的时候（详见测试）。

        x 上的 ±1 抖动照抄 `pirate_loop.slow_drag`：只抖垂直于榜单滚动方向的那一轴，
        步长二十几个像素，抖 1px 不会让路径回头。
        """
        import random

        if self._origin is None:
            raise RuntimeError("没有按下就移动：这一拖没有参照系，不动手")
        origin_x, origin_y = self._origin
        self._driver._gui.moveTo(  # noqa: SLF001 - 同上
            origin_x + x + random.randint(-1, 1),
            origin_y + y,
            random.uniform(0.02, 0.05),
        )

    def release(self) -> None:
        """松手。没按过就什么都不做。

        `ranking_nav._slow_drag` 在 `finally` 里调它，而 `press` 在 `try` 外面
        ——`press` 还没碰鼠标就失败（抢不到前台）时，这里会在一次都没按下的情况下
        被调用。那时候发 mouseUp 会**把用户自己正按着的拖动给松开**。

        `mouseUp` 抛了就不清状态，好让下一次 `release` 还能再试一遍。
        """
        if self._origin is None:
            return
        self._driver._gui.mouseUp()  # noqa: SLF001 - 同上
        self._origin = None

    def hover(self, x: int, y: int) -> None:
        """把指针挪到某处，**不按下**。滚轮盲滚之前用它落点。

        ⚠️ **不能借 `move_to` 干这件事**：那是分步慢拖的途中一步，没按下就调会被
        它的守卫拦掉（「没有按下就移动：这一拖没有参照系」）。那道守卫是对的——
        它保证一次拖动的所有中间点共用按下时的那套原点。

        ⚠️ **为什么盲滚必须先落点**：浏览器把滚轮事件路由给**指针底下**的元素。
        `open_military_ranking` 之后指针停在 `MILITARY_TAB`(1084, 212)，而榜单从
        `ROW_FIRST_Y`(257) 才开始——差 45 像素，事件全喂给页签条，榜单一行不动，
        而日志照样报「盲滚 700 行」（那个数是按格数换算的，不是量出来的）。
        2026-08-22 生产事故就是这么来的。

        这里**抢前台**：它在一次拨格循环之前只做一次，付得起 `focus()` 的代价，
        而拨格循环里反而不能抢（退避重试最多 4.5 秒，塞不进 16ms）。
        """
        import random

        self._driver.focus()
        origin_x, origin_y = self._driver.origin()
        self._driver._gui.moveTo(  # noqa: SLF001 - 同上
            origin_x + x, origin_y + y, random.uniform(0.2, 0.4)
        )

    def wheel_notch(self) -> None:
        """往下滚**一格**。盲滚段唯一的动作原语。

        ⚠️ **`pyautogui.scroll(n)` 在 Windows 上把 `n` 原样当 `dwData` 传给
        `mouse_event`，不乘 120。** 所以这里发的是 `-WHEEL_DELTA`(-120) 而不是
        `-1`——发 `-1` 只是 1/120 格，实测 80 次只走 0–3 行，**而它看起来完全
        正常**：事件发出去了，底层鼠标钩子也收到了，只是 `delta=-1`。
        （用户手动拨硬件滚轮，钩子看到的是 `delta=-120`。）

        ⚠️ **`PAUSE` 必须显式置 0。** 它默认 0.1 秒，`scroll()` 返回前会睡这么久，
        于是 `ranking_ui.WHEEL_GAP_S`(16ms) 被撑成 117ms/格；游戏做的是速度惯性
        滚动，那个密度攒不起动量，实测 80 格只走 2 行。症状和「发不足一格」
        一模一样，都是「拨了但没走」。

        ⚠️ **FAILSAFE 不许动。** 急停（鼠标甩到屏幕左上角）照常有效——
        盲滚一趟要发几百个事件，这是最需要留一条人工急停的地方。
        `load_pyautogui()` 把它置 True，这里只碰 `PAUSE`。

        ⚠️ **这里既不抢前台也不移鼠标**，两件事都不能放进一个 16ms 的循环里：
        `focus()` 抢不到会退避重试最多 4.5 秒，`origin()` 会在窗口不见时把游戏
        重新拉起来。落点由**上一个动作**负责——`spin_blind` 之前刚走完
        `open_military_ranking`，那一路的 `click` 走 `LiveDriver.click`
        （抢过前台），指针停在面板内部（`MILITARY_TAB` 或榜单行上）。
        """
        import pyautogui

        # ⚠️ **`PAUSE` 只在这一次调用里归零，出去必须还回去。**
        #
        # 2026-08-22 生产事故：原先这里是裸的 `pyautogui.PAUSE = 0`，全局赋值、
        # 永不恢复。盲滚一跑完，同进程后面**所有** pyautogui 调用都没有停顿了——
        # 包括检测段的分步慢拖。而本仓早就记着「一步到位的拖动会被游戏面板当成
        # 点击」：`_slow_drag` 一屏要发 1 次 press + 12 次 move_to + 1 次 release，
        # 14 个调用 × 0.1 秒 = 1.4 秒的节奏被抹平之后，游戏把整段拖动当成点击，
        # **列表一行都不动**。
        #
        # 生产日志的算术：出事那一轮检测段 2.95 秒/屏，而改动前的盲拖是 4.21 秒/屏，
        # 差 1.26 秒——正是那 14 个调用的 PAUSE 被吃掉的量。症状是「翻了 30 屏、
        # 名字列重合率 0.97」，也就是翻了个寂寞。
        previous = pyautogui.PAUSE
        try:
            pyautogui.PAUSE = 0
            pyautogui.scroll(-WHEEL_DELTA)
        finally:
            pyautogui.PAUSE = previous


# -- 扫描循环 ------------------------------------------------------------------


@dataclass
class ScanOutcome:
    coordinate: Coordinate
    panel: PlanetPanel
    confirmed: bool
    seconds: float


def scan_one(
    navigator: SystemNavigator,
    ocr: Any,
    coordinate: Coordinate,
    *,
    debug_dir: Path | None,
    attempts: int = READ_ATTEMPTS,
) -> ScanOutcome:
    """扫一个坐标；坐标核对不过就重来，重来仍不过才算失败。

    实测 `2:2:11` 被读成 `[2:2:1]`——相邻的两个 1 粘在一起少读一位。核对是对的
    （读回来的不是请求的那个就不能入库），但**只读一次就放弃会留下静默缺口**：
    这个坐标既没入库，游标也会被后面成功的坐标带过去，再也不会回来。

    核对通过时要 `navigator.confirm()`：那次核对本身就是导航栏的回读证据，
    导航器只信这种有证据的记忆（见 `SystemNavigator` 的类注释）。不调的话，
    同一恒星系内连扫 16 个位每一位都要重设三个字段——每位白花约 6 秒。
    """
    started = time.perf_counter()
    outcome: ScanOutcome | None = None
    requested = f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}"
    for attempt in range(attempts):
        if attempt:
            # 重来一次要把三个字段都重设：读不出可能是因为根本没跳过去。
            navigator.invalidate()
        navigator.goto(coordinate)
        image = navigator.driver.capture()
        # 先在同一张截图上换放大档位——位 11 的失败是读不出，不是没跳过去。
        panel = read_panel_confirming(crop_reader(image, ocr), requested)
        outcome = ScanOutcome(
            coordinate, panel, panel.confirms(requested), round(time.perf_counter() - started, 2)
        )
        if outcome.confirmed:
            navigator.confirm(coordinate)
            return outcome
        if debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            image.save(debug_dir / f"unconfirmed-{requested.replace(':', '-')}-{attempt + 1}.png")
    assert outcome is not None  # attempts >= 1
    return outcome


#: 底部导航条**只取文字那一行**。连着图标一起裁会把图标读成拉丁噪声，
#: 同一屏两次读出 '区 Y ASA 商店 o j' 和 '6S BOD ®@'——前者恰好含「商店」而后者不含，
#: 于是在线的会话时好时坏地被判成「认不出」。
NAV_TEXT_ROI = (800, 878, 1215, 908)
NAV_TEXT_UPSCALE = 3


#: START 按钮所在的横条。整屏 OCR 读不出 START——那是压在星空上的半透明大字，
#: psm 6/11/12 全军覆没；紧凑裁剪 + 3× + 二值化才读得出来。
#: 「进入」/ START 定位读几帧、每帧之间等多久。
#:
#: 这两个页面都在做明暗动画，单帧是抛硬币——理由整段写在
#: `make_session_keeper._locate_confirming` 上。取 5 × 0.8 秒 ≈ 最多多花 3.2 秒，
#: 而它挡住的是「整个进程抛异常退出」，挂机时那就是一整轮没了。
ENTRY_LOCATE_ATTEMPTS = 5
ENTRY_LOCATE_WAIT_S = 0.8

START_ROI = (820, 745, 1100, 830)
START_UPSCALE = 3
START_THRESHOLD = 180

#: 认 START 的**配方阶梯**：逐个试，读到 `START` 就算，一个都读不到才返回 None。
#:
#: ⚠️ **2026-08-15 凌晨这条把整晚堵死了。** 会话掉回 START 页，而
#: `START_THRESHOLD = 180` 在那一屏上**把字二值化没了**（读到空串），于是
#: `click_start` 抛「要点 START 时却读不到」，补录失败，而失败的补录反过来
#: 堵住调度器——一个任务都起不来，整夜空转。
#:
#: 在那张真图上量到的：
#:
#:     现行 ROI + 阈值 180   ''          ← 坏的就是它
#:     现行 ROI + 阈值 160   'START'
#:     紧 ROI  + 不二值化    'START'
#:     紧 ROI  + 阈值 140    'START'
#:
#: **不直接把 180 改成 160**：只有一张样本，而 180 当初是为某种渲染调出来的，
#: 换掉可能把那一档弄坏。阶梯则是加法——旧配方仍排第一，读不到才往下走。
#: 同一条道理：空结果不是证据（`preset_picker.read_names_confirming` 那条）。
START_RECIPES: tuple[tuple[tuple[int, int, int, int], int, int | None], ...] = (
    (START_ROI, START_UPSCALE, START_THRESHOLD),
    (START_ROI, START_UPSCALE, 160),
    ((845, 755, 1075, 815), 3, None),
)

#: 入口页（语言选择页）的标题与「进入」按钮。两处都读得很干净，不需要二值化。
#: 掉线弹窗的正文行与那个绿色 ✓（截图 client 空间，实机量于 2026-08-09）。
#:
#: 弹窗只有一个按钮，所以按钮位置写死；但**是否要点它由那行字决定**——
#: 读到「连接已断开」才点，读不到就停。绿色像素只用来量位置，不作判据：
#: 派遣界面上的绿色 ✓ 长得一模一样，靠颜色认会在派遣页上点出一发舰队。
#:
#: 同一块 ROI 也用来认「无法重新连接」那一种（会话已死，只能关窗重开）。
#: 两种弹窗长得一样、正文在同一行，所以只读一次、由 `classify_screen` 分流。
DISCONNECT_TEXT_ROI = (780, 440, 1140, 500)
DISCONNECT_BUTTON = (960, 583)
DISCONNECT_UPSCALE = 3

#: 服务器维护公告：标题横栏与「知道了」按钮。实机 2026-08-15 03:30 量的
#: （`var/logs/rankv/B0-dialog.png`）。
#:
#: 标题**不能**和掉线弹窗共用 `DISCONNECT_TEXT_ROI`：那块 ROI 在 y=440–500，
#: 而公告在那个位置上是正文（整段话，OCR 出来碎）。标题「服务器维护」在
#: y=295–325 一条独立横栏里，读得稳。
MAINTENANCE_TEXT_ROI = (750, 295, 1170, 325)
MAINTENANCE_BUTTON = (958, 783)
MAINTENANCE_UPSCALE = 3

#: 判定当前是哪一屏时最多取几帧。入口页在做明暗动画，实测约一半的帧什么都读不出
#: （见 `observe` 的注释）。4 帧足够：实测空帧与好帧大致交替，连着 4 帧全空的概率
#: 可以忽略，而代价只有认不出时才付。
OBSERVE_FRAMES = 4

#: 两帧之间隔多久。取 0.9s 是为了不和动画同频——固定短间隔有可能每次都踩在同一相位上。
OBSERVE_FRAME_GAP_S = 0.9

#: 认不出画面时，隔多久才肯再记一次**带图的**证据。
#:
#: ⚠️ **限流不是省空间，是防刷爆。** 2026-08-17 00:17--00:19 实机：认不出的状态
#: 持续了一个多小时，光那两分钟就打了 25 条「START 三个配方都读不出来」——每 2 秒
#: 一条。同样的频率往库里写缩略图，一小时就是上千张。
#:
#: 取 120s：runner 每轮存活通常只有几秒到几分钟，所以实际效果是**一轮最多一张**，
#: 既够诊断又不会堆积。
#:
#: 分类（2026-08-17 审计）：**低优先级旋钮**——理论上跟磁盘/库容量和排障需求有关，
#: 但它的取值只要「一轮最多一张」这个效果成立就够了，而 runner 的存活时长不由
#: 用户配置决定。没做成可配置：加一个没人会去动的框，只会让配置页更难读。
UNRECOGNISED_EVIDENCE_INTERVAL_S = 120.0

#: 缩略图宽度。480 px 够看清「这是哪一屏、导航条在不在、浮层盖住了没有」，
#: 一张 PNG 几十 KB，base64 之后仍在 `payload_json` 扛得住的量级。
#: 原图 1920 宽存进库一张就近 2 MB，不合适。
EVIDENCE_THUMBNAIL_WIDTH = 480

#: 上一次记证据的时刻（`time.monotonic`）。进程级，重启即清零——这正好，
#: 每一轮 runner 都值得留一张。
_last_evidence_at: float | None = None


def thumbnail_base64(image: Any, width: int = EVIDENCE_THUMBNAIL_WIDTH) -> str:
    """整帧缩到 `width` 宽的 PNG，base64。失败返回空串——证据不许把链路弄死。"""
    import base64
    import io

    try:
        scaled = image
        if image.width > width:
            height = max(1, round(image.height * width / image.width))
            scaled = image.resize((width, height))
        buffer = io.BytesIO()
        scaled.convert("RGB").save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception:  # noqa: BLE001 - 见 docstring：诊断路径不许抛
        return ""


def record_unrecognised_screen(
    image: Any, *, nav_text: str, entry_text: str, now: Callable[[], float] = time.monotonic
) -> bool:
    """把「这一屏认不出」的证据**连图一起**写进 `system_log`。返回是否真的写了。

    ⚠️ **2026-08-17 的教训：只记结论不记证据，等于没记。** 那一夜日志里翻来覆去
    只有一句 `unrecognised screen`，而真正的答案（导航条读到的是拉丁噪声 →
    中文语言包没装）一个字都没留下。查了一个多小时，靠的是人肉在另一台机器上
    手工跑探针把 OCR 读数打出来。

    所以这里记三样，缺一不可：

    - **画面尺寸**：一眼排除窗口没最大化 / 缩放不对 / 抓错窗口。
    - **导航条与入口标题的 OCR 原文**：中文读成拉丁字母，就是语言包问题；
      读成空，才是 ROI 落偏或者被浮层盖住。这两种的善后完全不同。
    - **一张缩略图**：跨机排障时对方传图不方便（用户口径 2026-08-17），
      图进库我这边直接查得到。

    图存进 `payload_json` 而不是 `artifacts` 表：后者存的是**路径**，而路径只在
    出事那台机器上有意义，跨机排障时等于没有。
    """
    global _last_evidence_at
    moment = now()
    if (
        _last_evidence_at is not None
        and moment - _last_evidence_at < UNRECOGNISED_EVIDENCE_INTERVAL_S
    ):
        return False
    _last_evidence_at = moment
    size = getattr(image, "size", None)
    record_system_log(
        "WARNING",
        "tools.scan_coordinates",
        f"画面认不出：尺寸 {size}；导航条读到 {nav_text!r}；入口标题读到 {entry_text!r}",
        payload={
            "capture_size": list(size) if size else None,
            "nav_text": nav_text,
            "entry_title_text": entry_text,
            "thumbnail_png_base64": thumbnail_base64(image),
        },
    )
    return True


ENTRY_TITLE_ROI = (780, 305, 1140, 355)
ENTRY_BUTTON_ROI = (999, 385, 1118, 430)
ENTRY_UPSCALE = 3
ENTRY_BUTTON_TEXT = "进入"


def make_session_keeper(
    driver: LiveDriver,
    ocr: Any,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    restart_window: Callable[[], None] | None = None,
) -> Any:
    """巡检用的会话守护。

    **只在真的读到 START 时才点 START**：判据来自这一屏本身，而不是「不在游戏里就多半是它」。
    认不出的画面一律停止——可能是维护公告或弹窗，乱点会误触派遣、删信或领奖。

    ``restart_window`` 是「会话已死时关窗重开」这个动作。它是**唯一**会真的
    关窗口、真的拉起 Chrome 的入口，做成参数就是为了让测试注入一个假的——
    单元测试绝不许真的开关窗口。默认接真实现。
    """
    from evo_helper.game.session_keeper import ScreenState, SessionKeeper, classify_screen

    def start_button(image: Any) -> tuple[int, int] | None:
        """在 START 横条里定位按钮；**逐个配方试**，全读不到才返回 None（于是不点）。

        阶梯的理由见 `START_RECIPES`：单一配方在 2026-08-15 凌晨读空，
        把整晚堵死了。
        """
        seen: list[str] = []
        for roi, upscale, threshold in START_RECIPES:
            text = ocr(image.crop(roi), digits=False, upscale=upscale, threshold=threshold)
            seen.append(text)
            if "START" in text.upper():
                left, top, right, bottom = START_ROI
                return ((left + right) // 2, (top + bottom) // 2)
        say(f"  START 三个配方都读不出来：{seen}")
        return None

    def entry_button(image: Any) -> tuple[int, int] | None:
        """在入口页上定位「进入」；读不到就返回 None（于是不点）。"""
        text = ocr(image.crop(ENTRY_BUTTON_ROI), digits=False, upscale=ENTRY_UPSCALE)
        if ENTRY_BUTTON_TEXT not in text:
            return None
        left, top, right, bottom = ENTRY_BUTTON_ROI
        return ((left + right) // 2, (top + bottom) // 2)

    def disconnect_screen(image: Any) -> ScreenState | None:
        """掉线弹窗在不在，以及是哪一种；都不是就返回 None。

        两种弹窗（可恢复的「连接已断开」、不可恢复的「无法重新连接」）长得一样、
        正文在同一行，只读一次、由 `classify_screen` 按文字分流。
        """
        text = ocr(image.crop(DISCONNECT_TEXT_ROI), digits=False, upscale=DISCONNECT_UPSCALE)
        state = classify_screen(text)
        if state in (ScreenState.DISCONNECTED, ScreenState.DEAD_SESSION):
            return state
        return None

    def maintenance_notice(image: Any) -> ScreenState | None:
        """服务器维护公告在不在。不是就返回 None。

        ⚠️ **2026-08-15 03:30 实机：这一屏把整晚堵死了。** 服务器停机维护，
        公告盖在 START 页上，而 `START_ROI` 那个位置上坐着的是公告的「知道了」
        ——`start_button` 于是一遍遍读到「知道了」、一遍遍判「读不出 START」，
        bot 链路空转了二十分钟，一发都没派。
        """
        text = ocr(image.crop(MAINTENANCE_TEXT_ROI), digits=False, upscale=MAINTENANCE_UPSCALE)
        return ScreenState.MAINTENANCE if classify_screen(text) is ScreenState.MAINTENANCE else None

    def dismiss_notice() -> None:
        # 同 `dismiss_disconnect`：**先回读确认公告真的在**，再点。
        # 认不出就停止，而不是朝固定坐标乱点。
        if maintenance_notice(driver.capture()) is not ScreenState.MAINTENANCE:
            raise RuntimeError("要关维护公告时却读不到标题；停止而不是往固定坐标乱点")
        driver.click(*MAINTENANCE_BUTTON, label="知道了")

    def observe() -> ScreenState:
        """多取几帧再下结论。**单帧在会动的页面上是抛硬币。**

        实机（2026-08-11 07:16）：入口页在做明暗动画，连读 6 帧的结果是

            第0帧 title='ETERNAL VOID' 进入='进入'   nav=''
            第1帧 title=''             进入=''      nav='>  =.  _'
            第2帧 title='ETERNAL VOID' 进入='进入'   nav=''
            第3帧 title=''             进入=''      nav='>  =.  _'

        一半的帧什么都读不出来。落在空帧上就判 UNKNOWN → 守护报「认不出的画面」
        → runner 拒绝开工 → 连撞三次，bot 和扫描两条链路双双被自动停用。而画面
        其实好好的，人手按同样的判据、多取几帧就走回游戏里了。

        只对 UNKNOWN 重取，**没有放松「认不出的画面一律停止」**：每一帧都读不出
        才返回 UNKNOWN。这也顺带压住了另一个坑——入口页底下透着一层淡淡的
        START，空帧上 `entry` 认不出时会退去读它，偶尔真能读成 START，于是在
        入口页上去点 START。等到一帧读得清再判，这个歧义就不存在了。
        """
        for attempt in range(OBSERVE_FRAMES):
            state = observe_once()
            if state is not ScreenState.UNKNOWN:
                return state
            if attempt + 1 < OBSERVE_FRAMES:
                sleep(OBSERVE_FRAME_GAP_S)
        # 连 `OBSERVE_FRAMES` 帧都认不出，才留证据：中间那几帧本来就常是空帧
        # （见上面的实测），逐帧都记等于把正常的过渡态也当成故障存图。
        if last_evidence:
            record_unrecognised_screen(
                last_evidence["image"],
                nav_text=last_evidence["nav_text"],
                entry_text=last_evidence["entry_text"],
            )
        return ScreenState.UNKNOWN

    #: 最后一次「认不出」时手上的那一帧与它的读数，供 `observe` 收尾时留证据。
    last_evidence: dict[str, Any] = {}

    def observe_once() -> ScreenState:
        image = driver.capture()
        # **掉线要排在导航条之前判。** 弹窗是浮层，底下的导航条还在画面上，
        # 「商店/联盟」照样读得出来——先判导航条就会把死会话认成在线，
        # 之后每一步点击都石沉大海，而且全程不报错。实机上确认过这一屏。
        # ⚠️ 维护公告排在最前，理由同下面掉线那一段：它是浮层，底下的 START
        # 与导航条照样读得出来，后判就会把一台停机的服务器认成「在 START 页上」。
        notice = maintenance_notice(image)
        if notice is not None:
            return notice
        popup = disconnect_screen(image)
        if popup is not None:
            return popup
        nav_text = ocr(image.crop(NAV_TEXT_ROI), digits=False, upscale=NAV_TEXT_UPSCALE)
        state = classify_screen(nav_text)
        if state is ScreenState.IN_GAME:
            return state
        # 顺序要紧：**先判入口页再判 START**。入口页浮在 START 页之上，
        # 底下那个 START 仍在画面里；反过来判就会在入口页上去点 START。
        entry_text = ocr(image.crop(ENTRY_TITLE_ROI), digits=False, upscale=ENTRY_UPSCALE)
        entry = classify_screen(entry_text)
        if entry is ScreenState.ENTRY:
            return entry
        result = ScreenState.START if start_button(image) is not None else state
        if result is ScreenState.UNKNOWN:
            last_evidence.update(image=image, nav_text=nav_text, entry_text=entry_text)
        return result

    def _locate_confirming(find: Callable[[Any], tuple[int, int] | None]) -> tuple[int, int] | None:
        """多取几帧再说「找不到」。**入口页和 START 页都在做动画。**

        `observe()` 早就这么干了（它的注释里记着实测：连读 6 帧，第 0 帧读到
        `进入`、第 1 帧读到空），但这两个**点击**的定位器一直只读一帧——于是
        `classify_screen` 说「这是入口页」而 `click_entry` 说「找不到进入」，
        两个读屏器对同一个页面结论相反。

        实机 2026-08-14 一小时内撞了三次：两次「要点『进入』时却读不到它」、
        一次「要点 START 时却读不到它」。而当场手工用同一套配方读同一个 ROI，
        **十帧十中**——定位器没毛病，是它读的那一帧正好在过渡。

        每一次失败的代价是整个进程抛异常退出：挂机时这一下就是一整轮没了。
        """
        for attempt in range(ENTRY_LOCATE_ATTEMPTS):
            spot = find(driver.capture())
            if spot is not None:
                return spot
            if attempt + 1 < ENTRY_LOCATE_ATTEMPTS:
                driver.wait(ENTRY_LOCATE_WAIT_S)
        return None

    def click_start() -> None:
        spot = _locate_confirming(start_button)
        if spot is None:
            raise RuntimeError("要点 START 时却读不到它；停止而不是往固定坐标乱点")
        driver.click(*spot, label="START")

    def click_entry() -> None:
        spot = _locate_confirming(entry_button)
        if spot is None:
            raise RuntimeError("要点「进入」时却读不到它；停止而不是往固定坐标乱点")
        driver.click(*spot, label=ENTRY_BUTTON_TEXT)

    def dismiss_disconnect() -> None:
        # **只在读到可恢复那一种时才点。** 会话已死的那一屏走的是关窗重开，
        # 不是点这个 ✓——点了也回不去，白白在一个死页面上留下一次点击。
        if disconnect_screen(driver.capture()) is not ScreenState.DISCONNECTED:
            raise RuntimeError("要关掉线弹窗时却读不到那行字；停止而不是往固定坐标乱点")
        driver.click(*DISCONNECT_BUTTON, label="确认掉线弹窗")

    def restart_game_window_now() -> None:
        from evo_helper.game.game_window import restart_game_window

        restart_game_window()

    return SessionKeeper(
        observe=observe,
        click_entry=click_entry,
        click_start=click_start,
        dismiss_disconnect=dismiss_disconnect,
        dismiss_notice=dismiss_notice,
        restart_window=restart_window or restart_game_window_now,
        log=say,
        clock=clock,
        sleep=sleep,
    )


def dismiss_overlays_if_unrecognised(session: Any, driver: Any, keeper: Any) -> Any:
    """`UNKNOWN` 先当成「浮层压着导航条」处理：关掉浮层再巡检一次。

    `classify_screen` 靠底部导航条的字判 IN_GAME，而信箱、飞行中列表、派遣面板
    都把它盖住。真掉线时画面是 ENTRY / START / DISCONNECTED，**落不到 UNKNOWN**。

    ⚠️ **「所以 UNKNOWN 只剩浮层这一种解释」——这句话 2026-08-17 被推翻了。**
    登录/加载翻页的那几秒同样落到 UNKNOWN（导航条读到噪声、入口标题读到残片），
    而那一档的正解是**等**，不是关浮层、更不是关窗重开。这一级仍然排在最前，
    因为浮层是常见的那一种、而且几秒就能证否；等待那一级紧随其后，
    见 `wait_for_login_if_unrecognised`。

    实机（2026-08-11 02:38）：上一条链路把游戏停在一个面板上，扫描开工时读到
    UNKNOWN，1.5 秒就「安全停止」并返回 1；连着三次，调度器把扫描整条**自动停用**。
    日志里只有三行「会话不可用：unrecognised screen」，而会话好好的。

    ⚠️ **「那个位置在恒星系视图上什么都不是，点空无害」这句话 2026-08-18 被推翻了。**
    实拍（`var/logs/atk-0-panel.png`、`plist-0.png`）里，恒星系视图上 (750, 71)
    正压在导航栏第一个输入框「银河系」上；星球地表上是等级徽章那一格
    （`var/logs/rank-closed.png`）；而实机 10:04/10:05 那两次，画面上是**军力排行榜**。
    所以现在这一下的准入条件是**先认出那个 ✕**（`game.overlay.close_button_visible`），
    认不出就一下都不点，如实往下走恢复阶梯的下一级。

    点击动作本身在 `game.overlay.dismiss_overlays`——攻击链路（`game.planet_list`）
    用的是同一份。这里只负责「什么时候算认不出」和「关完再问一次守护」。
    """
    from evo_helper.game.session_keeper import ScreenState

    if session is None or session.state is not ScreenState.UNKNOWN:
        return session
    say("画面认不出（多半是浮层）；关掉浮层后重新巡检")
    # 每关一下就重新巡检一次，认回来了就停手——用一格可变单元把结局带出闭包。
    latest = [session]

    def cleared() -> bool:
        latest[0] = keeper.ensure_connected(force=True)
        return latest[0] is None or latest[0].state is not ScreenState.UNKNOWN

    def see_close_button() -> bool:
        capture = getattr(driver, "capture", None)
        if not callable(capture):
            return False  # 看不到画面与「看到了但不是 ✕」处置相同：不点。
        look = look_at_close_button(capture())
        say(f"  关闭键回看：{'认出 ✕' if look.visible else '认不出 ✕'}（{look.as_payload()}）")
        return look.visible

    outcome = dismiss_overlays(driver, see_close_button=see_close_button, is_clear=cleared)
    if not outcome.recognised:
        say("  关闭键那个位置上认不出 ✕；一下都不点，交给恢复阶梯的下一级")
    return latest[0]


def wait_for_login_if_unrecognised(session: Any, keeper: Any) -> Any:
    """恢复阶梯的第三级：认不出**先当成「登录还没走完」等一会儿**，再谈关窗重开。

    ⚠️ **2026-08-17 实机：登录流程更新之后，一个正常的中间态被当成了故障。**
    现象、日志原文、判据与超时上限的取值依据，整段在
    `game.session_keeper.LOGIN_SETTLE_TIMEOUT_S`。用户口径：
    「这里应该是等待变更为 start」。

    **为什么排在关浮层之后**：浮层是 UNKNOWN 里更常见的那一种，而且几秒就能证否
    （点一下关闭键再问一次守护）。把 90 秒的等待挪到它前面，等于让每一次
    「上一轮把游戏停在某个面板上」都白等一分半。

    **为什么必须排在关窗重开之前**：这一级要挡住的正是那一下。登录才走到一半就
    把 Chrome 关掉重开，不但救不了，还会**把本来马上就好的会话亲手弄坏**，
    并且吃掉一次 3 次 / 1 小时的重开配额。

    **最坏情况下点到了什么：什么都没点。** 这一级只观察、只等待。所以它并没有
    放松「认不出的画面绝不点击」——它是整条阶梯里第二级完全不动手的。
    """
    from evo_helper.game.session_keeper import ScreenState

    if session is None or session.state is not ScreenState.UNKNOWN:
        return session
    return keeper.wait_for_known_screen()


def restart_if_still_unusable(session: Any, keeper: Any) -> Any:
    """恢复阶梯的最后一级：前面都试过了还是回不到游戏内，就关窗重开一次。

    **「上一轮没能正常收尾」是常态不是意外**：进程被强杀、断电、强制重启、用户
    点了任务管理器、runner 半路抛未捕获异常，都会留下一个停在半截画面上的窗口，
    甚至压根没有窗口——实测 `taskkill /F /T` 杀 runner 时把 Chrome 一起收走了
    （它是 `start-console.bat` 的子进程）。窗口不存在那一档由 `LiveDriver.window()`
    → `ensure_game_window()` 兜住；这里兜的是「窗口在、画面救不回来」。

    **每一级最坏情况下点到了什么**，这是本仓库那条底线（「认不出的画面绝不点击」）
    要求逐级说清的：

    1. `keeper.ensure_connected` —— 判据驱动的入口序列，认不出就停，不点。
    2. `dismiss_overlays_if_unrecognised` —— 左上角 (750, 71) 那个 ✕，而且
       **点之前先在那一帧上认出它**（`game.overlay`）；认不出就一下都不点。
       原先这里写的是「那个位置在恒星系视图上什么都不是，点空无害」——
       2026-08-18 实拍推翻了它，那儿坐着导航栏的「银河系」输入框。
    3. `wait_for_login_if_unrecognised` —— **什么都没点**，只等登录自己走完
       （上限 `LOGIN_SETTLE_TIMEOUT_S`）。它排在这里就是为了挡住下面那一下：
       登录才到一半就关窗重开，救不了，还会把本来马上就好的会话亲手弄坏。
    4. **这里** —— **什么都没点**。只往游戏窗口那个句柄送一个 `WM_CLOSE`
       （等同用户点右上角 ×，别的 Chrome 窗口不受影响），再由 `ensure_game_window`
       拉一个新的，然后重走判据驱动的入口序列。它是整条阶梯里唯一完全不在认不出
       的画面上动手的一级——所以它排在最后，而不是因为它最危险。

    **预算耗尽就停，不是接着重启**：`SessionKeeper` 的滚动配额是 3 次 / 1 小时，
    用尽后 `restart_and_reenter` 直接返回一个 `ready` 为假的结局，调用方照旧
    安全停止。服务端维护时每次巡检都会撞到这一屏，没有上限就成了「每 10 分钟
    关一次 Chrome 再开一次」，一直折腾到有人来看。
    """
    if session is None or session.ready:
        return session
    say(f"会话不可用：{session.detail}；关窗重开一次再试（兜底策略）")
    return keeper.restart_and_reenter(f"会话不可用：{session.detail}")


def exit_code_for_unusable_session(session: Any) -> int:
    """恢复阶梯走到头仍然回不到游戏内：这一轮该拿什么退出码收场。

    **判据是 `SessionKeeper` 的关窗重开配额**，理由整段在
    `domain.scheduler.exit_code_for_environment_fault`。一句话：配额是滚动窗口内
    有限的，所以「还有配额就报 75」这条判据必然有尽头——同一小时里最多三轮能这么
    收场，第四轮起 `restart_and_reenter` 直接被拒、配额恒为 0、退回 1，
    豁免照常攒，该停用的最终会停用。

    ⚠️ **不许无条件报 75。** 这一档和「抢不到前台」不同：走到这里说明这一轮
    已经关掉游戏窗口、重开过 Chrome 并且失败了。无条件豁免的话，调度器会每隔一个
    冷却再起一轮、再吃一次配额、再什么都不推进，而**再没有任何东西会最终把它
    停下来**——2026-08-17 那种故障就会从「26 分钟后被 6/6 豁免上限拦住」
    变成整夜静默空转。
    """
    return exit_code_for_environment_fault(
        recoverable=session is not None and session.restarts_left > 0
    )


def run_with_foreground_guard(body: Callable[[], int]) -> int:
    """跑一趟 runner；抢不到前台时按 `EXIT_ENVIRONMENT_BUSY` 收场。

    **无条件豁免，不看任何配额**，这一档和「会话恢复不了」正相反：抢不到前台
    的那条路**什么都不做**（不关窗、不重开、一次点击都不发），纯粹让路等用户
    不再用别的窗口。理由整段在 `game.game_window.ForegroundUnavailable`。

    只 catch 这一个类型。`GameWindowError` 的其余成员（窗口拉不起来、尺寸调不到
    标定值）**不在这一档**：那些不会因为用户放开鼠标就好。
    """
    try:
        return body()
    except ForegroundUnavailable as busy:
        say(f"{busy}")
        say(f"  这不算故障，本轮按「环境暂时不可用」收场（退出码 {EXIT_ENVIRONMENT_BUSY}）")
        return EXIT_ENVIRONMENT_BUSY


def run_scan(
    *,
    limit: int | None,
    debug_dir: Path | None,
    skip_scanned: bool,
    rescan_missing: bool = False,
    recheck_suspicious: bool = False,
    one_bot_per_system: bool = ONE_BOT_PER_SYSTEM,
) -> int:
    import ctypes

    # 系统缩放 125%：不声明 DPI 感知拿到的全是逻辑像素，窗口怎么调都对不上标定视口，
    # 而且看起来没有任何报错。
    # ctypes.windll 只在 Windows 上存在，动态取属性好让 Linux 上的 mypy 也能过。
    getattr(ctypes, "windll").shcore.SetProcessDpiAwareness(2)

    session_factory = create_session_factory(create_database_engine(Settings().database_url))
    repository = SqlAlchemyRepository(session_factory)
    priority_planets = configured_priority_planets(session_factory)
    run_id, cursor = ensure_run(session_factory, priority_planets=priority_planets)
    done = already_scanned(session_factory) if skip_scanned else set()

    say(f"运行实例 {run_id}")
    say(f"游标 {cursor if cursor is not None else '（尚未开始）'}；已确认坐标 {len(done)} 个")

    driver = LiveDriver()
    driver.window()  # 窗口不见了就在这一步拉起来，而不是等到第一次点击才失败
    ocr = make_ocr()
    navigator = SystemNavigator(driver)
    keeper = make_session_keeper(driver, ocr)

    def read_nav_labels() -> str:
        return str(ocr(driver.capture().crop(NAV_LABEL_ROI), digits=False, upscale=3))

    # **先确认会话还在，再谈切视图。** 顺序反了会这样：中断后重启时会话已经掉了，
    # 画面停在入口页或 START 页，导航栏标签自然读不到，`ensure_system_view` 就朝
    # 视图菜单坐标盲点三次然后放弃——**永远走不到能重连的 SessionKeeper**。
    # 实测这条路径把扫描卡了几千轮，日志里全是「切不到恒星系视图」，
    # 一次都没提过巡检；而且那三次点击本身就违反「认不出的画面绝不点击」。
    session = keeper.ensure_connected(force=True)
    session = dismiss_overlays_if_unrecognised(session, driver, keeper)
    session = wait_for_login_if_unrecognised(session, keeper)
    session = restart_if_still_unusable(session, keeper)
    if session is not None and not session.ready:
        code = exit_code_for_unusable_session(session)
        say(f"会话不可用：{session.detail}；安全停止（退出码 {code}）")
        return code
    if session is not None and session.reconnected:
        say("已重新登录")

    if not navigator.ensure_system_view(read_nav_labels):
        say("切不到恒星系视图；停止而不是往固定坐标乱点")
        return 1

    if recheck_suspicious:
        # 复核不推进游标，也不跳过「已扫过」——重点就是重读这些已入库的坐标。
        pending = suspicious_names(session_factory)
        say(f"复核模式：名字像 bot 却没判成 bot 的坐标有 {len(pending)} 个")
        done = set()
        coordinates: Iterator[Coordinate] = iter(pending)
    elif rescan_missing:
        # 补缺口时游标已经在前面了，不能再往前推——推了会把还没扫的坐标当成扫过。
        pending = list(
            missing_from_plan(
                session_factory,
                upto=cursor,
                one_bot_per_system=one_bot_per_system,
                priority_planets=priority_planets,
            )
        )
        say(f"补缺口模式：游标之前有 {len(pending)} 个坐标没入库")
        coordinates = iter(pending)
    else:
        coordinates = _planned(cursor, priority_planets=priority_planets)

    # 每系一个 bot：已经找到的系整个跳过。补缺口那条路径用的是同一个判据。
    found_systems = (
        systems_with_bot(session_factory) if one_bot_per_system and skip_scanned else set()
    )
    if one_bot_per_system:
        say(f"每系一个 bot：已定位 {len(found_systems)} 个系，这些系剩余的行星位不再扫")

    scanned = bots = rejected = skipped = 0
    consecutive_failures = 0
    for coordinate in coordinates:
        if limit is not None and scanned >= limit:
            say(f"已达本次上限 {limit}")
            break
        if (coordinate.galaxy, coordinate.system) in found_systems:
            # 这个系的 bot 已经找到了，剩下的位不用看。游标照推，续扫才不会回头。
            skipped += 1
            save_cursor(session_factory, run_id, coordinate)
            continue
        if (coordinate.galaxy, coordinate.system, coordinate.position) in done:
            save_cursor(session_factory, run_id, coordinate)
            continue

        outcome = restart_if_still_unusable(
            wait_for_login_if_unrecognised(
                dismiss_overlays_if_unrecognised(keeper.ensure_connected(), driver, keeper), keeper
            ),
            keeper,
        )
        if outcome is not None:
            if not outcome.ready:
                say(f"会话巡检未通过：{outcome.detail}；安全停止")
                return 1
            if outcome.reconnected and not navigator.ensure_system_view(read_nav_labels):
                say("重连后切不回恒星系视图；安全停止")
                return 1

        result = scan_one(navigator, ocr, coordinate, debug_dir=debug_dir)
        label = f"{coordinate.galaxy}:{coordinate.system}:{coordinate.position}"
        if not result.confirmed:
            consecutive_failures += 1
            rejected += 1
            say(
                f"{label} 坐标核对失败，原文 {result.panel.coordinate_text!r}"
                f"（连续 {consecutive_failures}）"
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                say("连续核对失败，画面多半已经不是面板；安全停止")
                return 1
            # 记下来，否则这个坐标既没入库、游标又被后面的成功坐标带过去，就此消失。
            record_gap(coordinate, result.panel.coordinate_text)
            navigator.invalidate()
            # 核对失败最常见的原因就是掉线。巡检十分钟才一次，等不到——
            # 这里立刻查一次，否则接下来又会在入口页上朝视图菜单盲点。
            dropped = restart_if_still_unusable(
                wait_for_login_if_unrecognised(
                    dismiss_overlays_if_unrecognised(
                        keeper.ensure_connected(force=True), driver, keeper
                    ),
                    keeper,
                ),
                keeper,
            )
            if dropped is not None and not dropped.ready:
                say(f"核对失败且会话不可用：{dropped.detail}；安全停止")
                return 1
            # 游戏会自己回到行星视图；先确认还在恒星系视图再接着扫。
            if not navigator.ensure_system_view(read_nav_labels):
                say("核对失败后切不回恒星系视图；安全停止")
                return 1
            continue

        consecutive_failures = 0
        name = result.panel.display_name
        repository.save_scan(
            CoordinateScan(
                run_id=run_id,
                coordinate=coordinate,
                scanned_at_utc=datetime.now(UTC),
                owner_name=name,
                is_bot=result.panel.is_bot,
                # 坐标经过「请求 vs 读回」双向一致校验，故为 1.0。
                confidence=1.0,
            )
        )
        if not rescan_missing and not recheck_suspicious:
            save_cursor(session_factory, run_id, coordinate)
        scanned += 1
        if result.panel.is_bot:
            bots += 1
            # 本系收工：每系一个 bot，剩下的位不用扫了。
            found_systems.add((coordinate.galaxy, coordinate.system))
            say(f"{label} BOT {name}  {result.seconds}s")
        else:
            say(f"{label} {name or '空位'}  {result.seconds}s")

    say(
        f"本次入库 {scanned} 条，其中 bot {bots} 个，核对失败 {rejected} 个，"
        f"按「每系一个 bot」跳过 {skipped} 个"
    )
    return 0


def _planned(
    cursor: Coordinate | None, *, priority_planets: tuple[Coordinate, ...] = ()
) -> Iterator[Coordinate]:
    from evo_helper.domain.scan_plan import CursorNotInPlanError

    try:
        yield from iter_scan_coordinates(after=cursor, priority_planets=priority_planets)
    except CursorNotInPlanError as exc:  # pragma: no cover - 计划改过才会走到
        raise SystemExit(f"{exc}；请确认分段是否变更后再续扫") from exc


def show_status() -> int:
    session_factory = create_session_factory(create_database_engine(Settings().database_url))
    priority_planets = configured_priority_planets(session_factory)
    run_id, cursor = ensure_run(session_factory, priority_planets=priority_planets)
    done = already_scanned(session_factory)
    total = total_coordinates(priority_planets=priority_planets)
    bounds = ScanBounds()
    say(f"运行实例 {run_id}")
    say(f"计划总量 {total:,} 个坐标（每系 {bounds.positions_per_system} 位，跳过 1–4）")
    say(f"游标 {cursor if cursor is not None else '（尚未开始）'}")
    say(f"已确认坐标 {len(done):,}（{len(done) / total:.4%}）")
    if priority_planets:
        say("优先星球：" + "、".join(map(str, priority_planets)) + "（各 ±100 系）")
    ranges = planned_segments(priority_planets=priority_planets)
    for index, (segment, start, end) in enumerate(ranges):
        span = f"{segment.galaxy}:{segment.first_system:03d}–{segment.last_system:03d}"
        say(f"  {index + 1:>2}. {span}  {start} → {end}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # 日志出口。装不上就是空操作，`say()` 照常打到控制台。
    install_runner_system_log()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="本次最多入库多少个坐标")
    parser.add_argument("--status", action="store_true", help="只看计划与游标，不动游戏")
    parser.add_argument(
        "--debug-dir", type=Path, default=Path("debug/scan"), help="核对失败时存图的目录"
    )
    parser.add_argument(
        "--no-skip-scanned", action="store_true", help="不跳过已确认过的坐标（用于强制复查）"
    )
    parser.add_argument(
        "--rescan-missing", action="store_true", help="只补游标之前没入库的坐标，不推进游标"
    )
    parser.add_argument(
        "--scan-full-systems",
        action="store_true",
        help="不套用「每系一个 bot」，每个系的 16 个位都扫（约耗时翻倍）",
    )
    parser.add_argument(
        "--recheck-suspicious",
        action="store_true",
        help="重读「名字像 bot 却没判成 bot」的坐标，不推进游标",
    )
    args = parser.parse_args(argv)

    if args.status:
        return show_status()
    return run_with_foreground_guard(
        lambda: run_scan(
            limit=args.limit,
            debug_dir=args.debug_dir,
            skip_scanned=not args.no_skip_scanned,
            rescan_missing=args.rescan_missing,
            recheck_suspicious=args.recheck_suspicious,
            one_bot_per_system=not args.scan_full_systems,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
