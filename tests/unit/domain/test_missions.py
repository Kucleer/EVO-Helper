"""钉死任务参数换算里几处一错就很危险、但错了也不会报错的地方。

三块：`--scout`/`--attack` 这类真正派遣舰队的开关必须原样出现在
生成的命令行里——漏掉不会报错，效果是配额被吃掉却毫无动静；恒星系范围的
钳制在盘面两端都要生效，不能只挡住下边界；命令行超长必须报错而不是被
Windows 静默截断成看起来成功的半条命令。
"""

from __future__ import annotations

import pytest

from evo_helper.domain.missions import (
    ORIGIN,
    MissionParamError,
    bot_command,
    bot_reconcile_command,
    bot_targets_in_range,
    pirate_command,
    pirate_systems,
    scan_command,
    wrap_system,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scan_bounds import SYSTEMS_PER_GALAXY


def test_pirate_systems_are_ordered_nearest_first() -> None:
    """由近到远；等距时小的在前——排序必须确定，否则测不住。"""
    systems = pirate_systems(ORIGIN, radius=2)

    assert systems == ((2, 137), (2, 136), (2, 138), (2, 135), (2, 139))


def test_a_radius_near_system_one_wraps_instead_of_being_clipped() -> None:
    """⚠️ **恒星系成环，所以半径要绕回去，不是在 1 上截断。**

    原先这里是 `max(1, s - r)`，于是从 2:2 半径 5 只给出 7 个系（1–7）。
    环上应该是 11 个：`496 497 498 499 · 1 2 3 4 5 6 7`。
    少掉的那 4 个不报错、日志里也看不出来，只是**永远不去打**。

    （实测依据见 `domain.distance` 模块头：从 2:137 打 2:499 只要 1969 秒，
    比 2:287 的 2042 秒还快——环上它是 137 步，不是 362 步。）
    """
    systems = [system for _galaxy, system in pirate_systems(Coordinate(2, 2, 1), radius=5)]

    assert len(systems) == 11
    assert set(systems) == {496, 497, 498, 499, 1, 2, 3, 4, 5, 6, 7}


def test_a_radius_near_the_last_system_wraps_the_other_way() -> None:
    """下边界绕过不代表上边界也绕了——原先那两头是各自独立的一次钳制。"""
    systems = [system for _galaxy, system in pirate_systems(Coordinate(2, 498, 1), radius=5)]

    assert len(systems) == 11
    assert set(systems) == {493, 494, 495, 496, 497, 498, 499, 1, 2, 3, 4}


def test_the_wrapped_radius_is_still_ordered_nearest_first() -> None:
    """绕回去的那些也要排在正确的位置上，不是缀在末尾。

    从 2:2 看，499 和 4 都是 2 步——它们必须**挨在一起**，而不是让 499
    因为数字大就掉到最后。等距时仍然小的在前。
    """
    systems = [system for _galaxy, system in pirate_systems(Coordinate(2, 2, 1), radius=3)]

    assert systems == [2, 1, 3, 4, 499, 5, 498]


def test_wrapping_lands_on_499_not_on_zero() -> None:
    """⚠️ **环的接缝在 499↔1，而 `0` 不是一个恒星系号。**

    `wrap_system` 少掉那个 `+1` 偏移的话，只有在结果正好是 499 的倍数时才露馅
    ——别处都对得上，于是很容易漏过去。用户看到的是「2:0 – 2:8」这种范围回显。
    """
    assert wrap_system(0) == SYSTEMS_PER_GALAXY
    assert wrap_system(SYSTEMS_PER_GALAXY) == SYSTEMS_PER_GALAXY
    assert wrap_system(SYSTEMS_PER_GALAXY + 1) == 1
    assert wrap_system(1) == 1
    assert wrap_system(-1) == SYSTEMS_PER_GALAXY - 1


def test_a_radius_past_half_the_ring_covers_the_galaxy() -> None:
    """环上没有「边」可以钳，所以半径超过半圈就是整圈——填大了不该报错。"""
    systems = pirate_systems(Coordinate(2, 137, 1), radius=SYSTEMS_PER_GALAXY)

    assert len(systems) == SYSTEMS_PER_GALAXY


def test_a_non_positive_radius_is_rejected() -> None:
    with pytest.raises(MissionParamError):
        pirate_systems(ORIGIN, radius=0)


def test_bot_targets_are_filtered_by_system_range() -> None:
    targets = (
        Coordinate(2, 99, 4),
        Coordinate(2, 100, 4),
        Coordinate(2, 150, 7),
        Coordinate(2, 201, 1),
        Coordinate(3, 150, 7),
    )

    kept = bot_targets_in_range(targets, galaxy=2, first_system=100, last_system=200)

    assert kept == (Coordinate(2, 100, 4), Coordinate(2, 150, 7))


def test_a_reversed_system_range_is_rejected() -> None:
    with pytest.raises(MissionParamError):
        bot_targets_in_range((), galaxy=2, first_system=200, last_system=100)


def test_an_empty_target_set_is_rejected_before_a_process_is_started() -> None:
    """范围内一个已记录 bot 都没有时，拉起一个必然空转的 runner 没有意义。"""
    with pytest.raises(MissionParamError):
        bot_command((), origin=ORIGIN)


def test_scan_command_is_the_full_argv() -> None:
    assert scan_command()[1:] == ["-u", "-m", "evo_helper.tools.scan_coordinates"]


def test_pirate_command_is_the_full_argv_including_the_action_flags() -> None:
    """`--scout --attack` 是真的动鼠标派舰队的开关，必须整条对比，不能只挑几个子串。

    只断言「`--systems` 在里面」测不出 `--attack` 被漏掉——那种情况下海盗
    只侦查不打，当天配额白白流失，而且不会有任何报错。
    """
    assert pirate_command(((2, 137),), origin=ORIGIN)[1:] == [
        "-u",
        "-m",
        "evo_helper.tools.pirate_loop",
        "--systems",
        "2:137",
        "--origin",
        "2:137:18",
        "--scout",
        "--attack",
    ]


def test_bot_command_is_the_full_argv_including_the_action_flags() -> None:
    """整条对比，不挑子串：`--attack` 漏掉不会报错，只是白跑一趟。

    ⚠️ **`--probe` 与 `--tier-thresholds` 必须不在里面。** bot 不再做攻击侦查、
    不再分档（用户口径 2026-08-13）：多传一个 runner 已经不认识的参数，
    argparse 会当场 `SystemExit(2)`，而调度器看到的只是「这条链路又崩了一次」。
    """
    assert bot_command((Coordinate(2, 137, 14),), origin=ORIGIN)[1:] == [
        "-u",
        "-m",
        "evo_helper.tools.bot_loop",
        "--targets",
        "2:137:14",
        "--origin",
        "2:137:18",
        "--attack",
    ]


def test_bot_report_reconciliation_has_no_targets_or_attack_capability() -> None:
    """到期战报回收不能顺手变成一轮攻击。"""
    command = bot_reconcile_command()

    assert command[-2:] == ["--reconcile", "--reconcile-only"]
    assert "--targets" not in command
    assert "--attack" not in command


def test_an_over_long_command_line_is_rejected_rather_than_truncated() -> None:
    """Windows CreateProcess 有 32767 字符上限。

    截断成「只打了前一半」比报错危险得多——它看起来成功了。
    """
    many = tuple(Coordinate(2, system, 1) for system in range(1, 4000))

    with pytest.raises(MissionParamError):
        bot_command(many, origin=ORIGIN)


def test_the_home_planet_is_resolved_in_exactly_one_place() -> None:
    """主星只能有一份解析。

    原先 `domain.missions`、`tools.pirate_loop`、`tools.scan_coordinates` 各写了
    一遍同一个坐标：改一次主星要改三处，漏掉任何一处的后果都是舰队从错误的
    星球出发，而三处彼此不核对，谁也不会报错。

    现在主星可配（换账号就得换），所以「同一个常量」变成了「同一个解析函数」：
    `domain.missions.ORIGIN` 只是默认值——`domain` 不许 import `config`——
    真正的取值由 `tools.scan_coordinates.origin()` 一处从 Settings 读。
    用 `is` 而不是 `==`：值相等只说明这一刻碰巧一样。
    """
    from evo_helper.tools import pirate_loop, scan_coordinates

    assert pirate_loop.origin is scan_coordinates.origin
    assert scan_coordinates.origin() == ORIGIN
