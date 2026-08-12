"""任务参数到命令行的换算。

三条链路的参数形状彼此不通：扫描不吃参数（它自己管计划和游标），
bot 要完整坐标，海盗要恒星系。换算集中在这里，纯函数，
不碰数据库也不碰进程——调度器起进程之前先在这里把参数校验完。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from evo_helper.domain.fleet_tier import TierThresholds
from evo_helper.domain.models import Coordinate

# 故意不从 scan_priority 导入：那边只是转手 import，没有再导出，
# strict mypy 的 no_implicit_reexport 会拒绝。直接从定义它的模块取。
from evo_helper.domain.scan_bounds import SYSTEMS_PER_GALAXY

#: 主星。原先在 `tools.pirate_loop` 与 `tools.scan_coordinates` 各写了一遍。
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


def pirate_command(systems: Sequence[tuple[int, int]]) -> list[str]:
    """海盗巡航命令行。

    `--scout --attack` 是这条命令会**真的动鼠标派舰队**的开关，不是可有可无的
    修饰参数——漏掉 `--attack` 只会侦查不会打，而且不报错、看着一切正常，
    代价是当天 32 次配额白白流失。
    """
    if not systems:
        raise MissionParamError("没有可打的恒星系")
    listed = [f"{galaxy}:{system}" for galaxy, system in systems]
    return _checked(
        [_PYTHON, "-u", "-m", "evo_helper.tools.pirate_loop", "--systems", *listed]
        + ["--scout", "--attack"]
    )


def bot_command(targets: Sequence[Coordinate], thresholds: TierThresholds) -> list[str]:
    """bot 攻击命令行。

    同 `pirate_command`：`--probe --attack` 是会真的派遣舰队的开关，
    必须原样出现在生成的 argv 里，不能被当作可省略的默认值。

    分档阈值也写进 argv 而不是让 runner 自己去查库，理由是这条命令行会原样存进
    `mission_runs.command`——事后翻账时「那一轮到底打了谁」全靠它，加上这三个数
    之后它还能回答「按哪三个数分的档」。而且这样一来，运行中就算有人改了库里的
    阈值，已经起来的那个子进程用的仍然是启动那一刻的取值。
    """
    if not targets:
        raise MissionParamError("该范围内没有已记录的 bot；先跑扫描")
    listed = [f"{item.galaxy}:{item.system}:{item.position}" for item in targets]
    edges = [str(edge) for edge in thresholds.edges]
    return _checked(
        [_PYTHON, "-u", "-m", "evo_helper.tools.bot_loop", "--targets", *listed]
        + ["--tier-thresholds", *edges]
        + ["--probe", "--attack"]
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
