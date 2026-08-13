"""任务参数到命令行的换算。

三条链路的参数形状彼此不通：扫描不吃参数（它自己管计划和游标），
bot 要完整坐标，海盗要恒星系。换算集中在这里，纯函数，
不碰数据库也不碰进程——调度器起进程之前先在这里把参数校验完。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from evo_helper.domain.models import Coordinate

# 故意不从 scan_priority 导入：那边只是转手 import，没有再导出，
# strict mypy 的 no_implicit_reexport 会拒绝。直接从定义它的模块取。
from evo_helper.domain.scan_bounds import SYSTEMS_PER_GALAXY

#: 主星。原先在 `tools.pirate_loop` 与 `tools.scan_coordinates` 各写了一遍。
#:
#: 它现在是**默认**出发星球：每个任务自己带一个 `origin`（`mission_tasks` 上的
#: 三列），没填时回落到这里。
ORIGIN = Coordinate(2, 137, 18)

#: 三条命令都用 sys.executable 而不是写死 "python"：写死会走 PATH 解析，
#: 调度器若在 venv 外的系统解释器下运行，拉起的 runner 就会跟着跑到系统
#: 解释器上，找不到本仓的依赖。sys.executable 保证子进程用的是同一个解释器。
_PYTHON = sys.executable

#: 命令行长度上限。Windows `CreateProcess` 的硬上限是 32767 字符，留出余量。
#: 超了要报错而不是截断——截断成「只打了前一半」看起来是成功的。
MAX_COMMAND_CHARS = 30000


class MissionParamError(ValueError):
    """任务参数不合格。调度器据此拒绝启动，而不是拉起一个注定空转的进程。"""


def check_origin_dispatchable(origin: Coordinate, current: Coordinate) -> None:
    """这个任务的出发星球，现在派得出去吗。

    ⚠️ **这是一道临时闸门，等「切换星球」实装就该整段删掉。**

    助手目前**不会在游戏里切换当前星球**：派遣面板上的出发地就是游戏此刻选中的
    那一颗，而那一颗一直是主星。任务上的 `origin` 是记账用的——它会原样写进
    `attack_intents.origin_*`，而战报认领正是靠「出发坐标 + 目标坐标 + 时间就近」
    配对的（`repository.append_report`）。

    所以放行一个和实际出发地不符的 `origin`，代价不是「打不到」，而是**账本在
    撒谎**：舰队从主星飞出去，台账上却写着从 9:250:8 出发，战报永远配不上那一发，
    飞行时间与航线钟也全按错的距离算。宁可在这里拒掉，让用户看见一句话。

    留成一个具名函数而不是散在几处 `if`：切换星球落地时，删掉的是这一个函数和
    它的调用点，而不是去几个文件里找那几行条件。
    """
    if origin != current:
        raise MissionParamError(
            f"出发星球 {origin} 与游戏当前选中的 {current} 不是同一颗，"
            f"而助手还不会在游戏里切换星球；照这样派出去，台账上的出发坐标会与"
            f"实际不符，战报也永远配不上。请先把出发星球改回 {current}。"
        )


def pirate_systems(origin: Coordinate, radius: int) -> tuple[tuple[int, int], ...]:
    """从主星向外排的恒星系清单，由近到远。

    等距时小的在前：排序必须是确定的，否则「上一轮打到哪了」无从谈起。
    越界的系号钳制到 `[1, SYSTEMS_PER_GALAXY]`——半径填大了应当是「到边为止」。
    """
    if radius < 1:
        raise MissionParamError(f"半径要大于 0（收到 {radius}）")
    low = max(1, origin.system - radius)
    high = min(SYSTEMS_PER_GALAXY, origin.system + radius)
    ordered = sorted(range(low, high + 1), key=lambda system: (abs(system - origin.system), system))
    return tuple((origin.galaxy, system) for system in ordered)


def bot_targets_in_range(
    targets: Sequence[Coordinate], *, galaxy: int, first_system: int, last_system: int
) -> tuple[Coordinate, ...]:
    """已记录的 bot 里落在这个恒星系区间内的那些。位次全要。"""
    if first_system > last_system:
        raise MissionParamError(f"恒星系区间首尾颠倒：{first_system} > {last_system}")
    return tuple(
        target
        for target in targets
        if target.galaxy == galaxy and first_system <= target.system <= last_system
    )


def scan_command() -> list[str]:
    """扫描不吃参数：它自己维护计划与游标（`tools.scan_coordinates`）。"""
    return _checked([_PYTHON, "-u", "-m", "evo_helper.tools.scan_coordinates"])


def pirate_command(systems: Sequence[tuple[int, int]], *, origin: Coordinate) -> list[str]:
    """海盗巡航命令行。

    `--scout --attack` 是这条命令会**真的动鼠标派舰队**的开关，不是可有可无的
    修饰参数——漏掉 `--attack` 只会侦查不会打，而且不报错、看着一切正常，
    代价是当天 32 次配额白白流失。

    `--origin` 是这一轮记账用的出发星球。**必须显式传**：runner 少了它会回落到
    `EVO_HELPER_ORIGIN`，于是任务上配的那颗和真正写进 `attack_intents` 的那颗
    可以是两颗不同的星球，而两者不一致时战报永远配不上。
    """
    if not systems:
        raise MissionParamError("没有可打的恒星系")
    listed = [f"{galaxy}:{system}" for galaxy, system in systems]
    return _checked(
        [_PYTHON, "-u", "-m", "evo_helper.tools.pirate_loop", "--systems", *listed]
        + ["--origin", str(origin), "--scout", "--attack"]
    )


def bot_command(targets: Sequence[Coordinate], *, origin: Coordinate) -> list[str]:
    """bot 攻击命令行。

    同 `pirate_command`：`--attack` 是会真的派遣舰队的开关，必须原样出现在生成的
    argv 里，不能被当作可省略的默认值。漏掉它只会「站过去看一眼」，不报错、
    看着一切正常，而这一轮一发都没打。

    ⚠️ **不再有 `--probe`，也不再有 `--tier-thresholds`。** 用户口径
    （2026-08-13）：bot 不做攻击侦查，直接用预设 BBB 打，平局就对同一坐标再打
    （有界，见 `domain.bot_round.MAX_ATTACKS_PER_TARGET`）。预设标题不可配，
    所以这条命令行上没有任何和「打得多狠」有关的参数了——
    `mission_runs.command` 仍然回答得了「那一轮到底打了谁」，那才是它的用途。

    `--origin` 同 `pirate_command`：这一轮的出发星球，显式传，不许让 runner 自己
    去猜。多个 bot 任务的区别就在这一个参数上——猜错了两个任务的账会记到一起。
    """
    if not targets:
        raise MissionParamError("该范围内没有已记录的 bot；先跑扫描")
    listed = [f"{item.galaxy}:{item.system}:{item.position}" for item in targets]
    return _checked(
        [_PYTHON, "-u", "-m", "evo_helper.tools.bot_loop", "--targets", *listed]
        + ["--origin", str(origin), "--attack"]
    )


def _checked(command: list[str]) -> list[str]:
    # 用「每段长度 + 1 个分隔空格」估算，不用 subprocess.list2cmdline 的真实
    # 转义长度：前者恒比后者略大（偏保守，宁可提前拒绝），且 30000 到
    # Windows 32767 的硬上限之间还留了 2767 的余量，够抵消这点高估。
    length = sum(len(part) + 1 for part in command)
    if length > MAX_COMMAND_CHARS:
        raise MissionParamError(
            f"命令行 {length} 字符，超过 {MAX_COMMAND_CHARS} 上限；缩小范围再试"
        )
    return command
