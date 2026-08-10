"""钉死任务参数换算里几处一错就很危险、但错了也不会报错的地方。

三块：`--scout`/`--attack`/`--probe` 这类真正派遣舰队的开关必须原样出现在
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
    bot_targets_in_range,
    pirate_command,
    pirate_systems,
    scan_command,
)
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scan_bounds import SYSTEMS_PER_GALAXY


def test_pirate_systems_are_ordered_nearest_first() -> None:
    """由近到远；等距时小的在前——排序必须确定，否则测不住。"""
    systems = pirate_systems(ORIGIN, radius=2)

    assert systems == ((2, 137), (2, 136), (2, 138), (2, 135), (2, 139))


def test_a_radius_past_the_edge_is_clamped_not_rejected() -> None:
    """半径填大了应当是「到边为止」，不是「不许开始」。"""
    systems = pirate_systems(Coordinate(2, 2, 1), radius=5)

    assert min(system for _galaxy, system in systems) == 1


def test_a_radius_past_the_upper_edge_is_clamped_too() -> None:
    """下边界钳过不代表上边界也钳了——两头各是一次独立的 min/max。"""
    systems = pirate_systems(Coordinate(2, 498, 1), radius=5)

    assert max(system for _galaxy, system in systems) == SYSTEMS_PER_GALAXY


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
        bot_command(())


def test_scan_command_is_the_full_argv() -> None:
    assert scan_command()[1:] == ["-u", "-m", "evo_helper.tools.scan_coordinates"]


def test_pirate_command_is_the_full_argv_including_the_action_flags() -> None:
    """`--scout --attack` 是真的动鼠标派舰队的开关，必须整条对比，不能只挑几个子串。

    只断言「`--systems` 在里面」测不出 `--attack` 被漏掉——那种情况下海盗
    只侦查不打，当天配额白白流失，而且不会有任何报错。
    """
    assert pirate_command(((2, 137),))[1:] == [
        "-u",
        "-m",
        "evo_helper.tools.pirate_loop",
        "--systems",
        "2:137",
        "--scout",
        "--attack",
    ]


def test_bot_command_is_the_full_argv_including_the_action_flags() -> None:
    assert bot_command((Coordinate(2, 137, 14),))[1:] == [
        "-u",
        "-m",
        "evo_helper.tools.bot_loop",
        "--targets",
        "2:137:14",
        "--probe",
        "--attack",
    ]


def test_an_over_long_command_line_is_rejected_rather_than_truncated() -> None:
    """Windows CreateProcess 有 32767 字符上限。

    截断成「只打了前一半」比报错危险得多——它看起来成功了。
    """
    many = tuple(Coordinate(2, system, 1) for system in range(1, 4000))

    with pytest.raises(MissionParamError):
        bot_command(many)


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
