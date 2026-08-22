"""任务参数到命令行的换算。

三条链路的参数形状彼此不通：扫描不吃参数（它自己管计划和游标），
bot 要完整坐标，海盗要恒星系。换算集中在这里，纯函数，
不碰数据库也不碰进程——调度器起进程之前先在这里把参数校验完。
"""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence

from evo_helper.domain.distance import system_gap
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
    """任务参数不合格。调度器据此拒绝启动，而不是拉起一个注定空转的进程。

    ⚠️ **它的后果是「自动停用」，不只是「这一轮不跑」。**
    `application.mission_scheduler` 接住它之后会调 `repository.disable_mission_task`：
    任务被停用、挂上 `disabled_reason`，而调度判据认的是
    `enabled and disabled_reason is None`——也就是说**用户不去页面上点一次「恢复」，
    它就永远不会再跑**（PR #161 只让「空闲航线不足」那一类自动恢复，其余仍需人工）。

    所以这个异常只许表达「**配置填错了**，重试一百次也是同一个下场」。任何
    「这会儿暂时没活干」都必须走 `MissionIdle`，否则一次正常的间歇会把整条链路
    静默地关掉一整夜。
    """


class MissionIdle(Exception):
    """这一轮没活干。**不是错误**，绝不能让它把任务停用。

    典型的一档：军力优先模式下，候选池里一个读数新鲜的目标都没有
    （`domain.target_order.fresh_targets` 全滤光了）。那时正确的动作是干净地
    什么都不做，把鼠标让给调度器的填空隙机制去跑军力榜扫描，把池子刷新——
    而不是拿旧读数去打，更不是把任务关掉。

    它刻意**不继承** `MissionParamError`（也不继承 `ValueError`）：继承的话，
    现有那几处 `except MissionParamError` 会顺手把它一起接住，接住之后干的正是
    `disable_mission_task`，等于这个类型白分了。

    正常情况下它甚至不该被抛出来：`domain.scheduler.has_work` 早在派遣之前就靠
    `targets_remaining` 把这条链路判成「没活干」了。它兜的是那之间的时间差
    ——事实是在锁外读的，读完到真的组命令行之间，最后一个新鲜目标可能刚好过期。
    """


class NoFreeLineError(MissionParamError):
    """这一刻没有空闲航线可派。**它会自己好起来，别当配置错误处理。**

    单独立一个类型，是为了让调用方能在**结构上**分清这一档和别的参数不合格：
    「范围里没有 bot」「命令行超长」要用户动手改配置，改之前重试一万次都一样；
    而航线是舰队飞回来就空出来的，纯时间判据（`storage.repository`
    的 `_still_holding_a_line`），没有任何人需要动手。

    ⚠️ **不许拿 `str(exc)` 里的中文去认它。** 那是给人看的一句话，改一次措辞
    判据就静默失效——静默的后果是任务停用之后再也没人放它出来。

    仍然继承 `MissionParamError`：既有的 `except MissionParamError` 一处都不用改，
    照旧接得住；只有想区分的那几处才去问 `isinstance`。
    """


# ⚠️ 这里原先有一道临时闸门 `check_origin_dispatchable`：助手还不会在游戏里切换
# 当前星球时，配一颗不是主星的出发星球会被当场拒掉。**它已经随「切换星球」实装
# 一起删掉了**（runner 开工时走 `game.planet_list.PlanetSwitcher` 真的切过去，
# 并回读派遣面板的「起点」确认）。不要照着旧注释把它加回来——加回来的效果是
# 「除主星以外的任务一律派不出去」，而那正是这一版要解决的问题。


def wrap_system(system: int) -> int:
    """把任意整数绕回 `[1, SYSTEMS_PER_GALAXY]`。恒星系首尾相接（见 `domain.distance`）。"""
    return (system - 1) % SYSTEMS_PER_GALAXY + 1


def pirate_systems(origin: Coordinate, radius: int) -> tuple[tuple[int, int], ...]:
    """从主星向外排的恒星系清单，由近到远。

    等距时小的在前：排序必须是确定的，否则「上一轮打到哪了」无从谈起。

    ⚠️ **半径是绕着环量的，不是在 `[1, 499]` 上截断的。**
    恒星系首尾相接（实测见 `domain.distance` 模块头：从 2:137 打 `2:499` 只要
    1969 秒，比 `2:287` 的 2042 秒还快——环上它只有 137 步而不是 362 步）。

    原先这里是 `max(1, s-r)` / `min(499, s+r)`，于是主星靠边时**半径会被悄悄砍掉一截**：
    从 2:2 半径 5 只给出 7 个系（1–7），而环上应该是 11 个（496–499 与 1–7）。
    少掉的那 4 个不会报错、日志里也看不出来，只是永远不去打。

    半径大到超过半圈（249）时自然覆盖整个银河——环上没有「边」可以钳。
    """
    if radius < 1:
        raise MissionParamError(f"半径要大于 0（收到 {radius}）")
    within_radius = [
        system
        for system in range(1, SYSTEMS_PER_GALAXY + 1)
        if system_gap(system, origin.system) <= radius
    ]
    ordered = sorted(within_radius, key=lambda system: (system_gap(system, origin.system), system))
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


def ranking_command(*, bot_limit: int | None = None, blind_rows: int | None = None) -> list[str]:
    """军力榜采集命令行。**没有 `--attack`。**

    这条链路只导航、只读、只入库——`LiveDriver()` 用默认的 `allow_actions=False`，
    结构上就没有派舰队的能力。列边界之类的都已经实机标定进 `game.ranking_ui`，
    命令行上留的覆盖参数是给调试用的，正常跑不传。

    ``bot_limit`` 是军力攻击批次所需的榜单目标数。传入时采够这一批就收工，
    让调度器立即转去攻击，而不是继续把整张榜翻完。

    ``blind_rows`` 是开榜后先无脑往下滚几**行**（攻击配置页上那个框）。
    **`None` 时命令行上不能出现 `--blind-rows`**：runner 那边的默认值
    （`game.ranking_ui.BLIND_SCROLL_ROWS`）才是「留空」的含义，在这里补一个
    「看起来一样」的数字送过去，日后调默认值就调不动了。

    ⚠️ **单位是行，不是屏**（口径改动 2026-08-22，盲滚段从慢拖换成滚轮）。
    滚轮没有「屏」这个概念，拨的是格；行是唯一同时量得住慢拖和滚轮的单位，
    也是采集那头真正记账的单位。屏版的 `--blind-scrolls` 仍留在
    `tools.ranking_scan` 上当回滚杠杆，但**调度器不再往命令行上放它**。

    它和 `scan_command` 同属**填空隙**那一档（`domain.scheduler.GAP_FILLERS`）：
    不占航线、没有完成态、排最后、攻击到点了随时可以把它抢占掉。
    """
    command = [_PYTHON, "-u", "-m", "evo_helper.tools.ranking_scan"]
    if bot_limit is not None:
        if bot_limit < 1:
            raise MissionParamError("军力榜采集数量必须至少为 1")
        command.extend(["--bot-limit", str(bot_limit)])
    if blind_rows is not None:
        # 0 合法（「一行都别盲滚」是最保守的取值），负数不是。
        #
        # ⚠️ **不设上界，一个都别加。** 用户口径（2026-08-22）：盲滚行数由用户定，
        # 助手不做越界判断。尤其**不许拿 `game.ranking_ui.FIRST_BOT_RANK`(587)
        # 当上界**——那个「bot 起点」是玩家改名伪装出来的（判据只看名字前缀
        # `bot_`，改名的真人一样命中），真 bot 区在更后面。拿一个被伪装污染的
        # 边界去拦，比不拦更坏：用户明明知道该填 700 却填不进去，而拦下来的
        # 那次还会因为 `MissionParamError` 把整条采集链路停用到手动恢复为止。
        if blind_rows < 0:
            raise MissionParamError("盲滚行数不能是负数")
        command.extend(["--blind-rows", str(blind_rows)])
    return _checked(command)


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


def bot_command(
    targets: Sequence[Coordinate],
    *,
    origin: Coordinate,
    presets: Mapping[Coordinate, str] | None = None,
    max_dispatches: int | None = None,
) -> list[str]:
    """bot 攻击命令行。

    同 `pirate_command`：`--attack` 是会真的派遣舰队的开关，必须原样出现在生成的
    argv 里，不能被当作可省略的默认值。漏掉它只会「站过去看一眼」，不报错、
    看着一切正常，而这一轮一发都没打。

    ⚠️ **不再有 `--probe`，也不再有 `--tier-thresholds`。** 用户口径
    （2026-08-13）：bot 不做攻击侦查，直接用预设 BBB 打。原先「平局就对同一坐标
    再打」那半条已按用户口径（2026-08-17）移除，每个目标本轮只打一发。
    预设标题不可配，所以这条命令行上没有任何和「打得多狠」有关的参数了——
    `mission_runs.command` 仍然回答得了「那一轮到底打了谁」，那才是它的用途。

    `--origin` 同 `pirate_command`：这一轮的出发星球，显式传，不许让 runner 自己
    去猜。多个 bot 任务的区别就在这一个参数上——猜错了两个任务的账会记到一起。
    """
    if not targets:
        raise MissionParamError("该范围内没有已记录的 bot；先跑扫描")
    # 同坐标把预设写进 argv，使命令台账能如实回放每一发用了哪个标题。
    # 区域攻击不传映射，runner 仍使用既有 BBB，避免被军力逻辑影响。
    listed = [
        f"{item.galaxy}:{item.system}:{item.position}={presets[item]}"
        if presets is not None and item in presets
        else f"{item.galaxy}:{item.system}:{item.position}"
        for item in targets
    ]
    command = [_PYTHON, "-u", "-m", "evo_helper.tools.bot_loop", "--targets", *listed] + [
        "--origin",
        str(origin),
    ]
    if max_dispatches is not None:
        if max_dispatches < 1:
            raise NoFreeLineError("空闲航线不足，暂不启动 bot 攻击")
        command += ["--max-dispatches", str(max_dispatches)]
    # `--attack` 历来在末尾；保留这一约定，既让运行台账可直接肉眼识别，也不破坏
    # 依赖该稳定 argv 形状的现有调用方。
    return _checked(command + ["--attack"])


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
