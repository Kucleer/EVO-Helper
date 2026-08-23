"""选靶窗口那两格改成**全局设置**之后的三条判据。

用户口径（2026-08-23）：「军力攻击的有效期 门限 改为全局设置，不再根据单个星系
进行调整」。搬家本身在集成层验（`tests/integration/application/test_mission_scheduler.py`
的 `test_the_old_task_level_window_keys_are_ignored_and_shouted_about`）；这里钉的是
三个**不需要库**就能量的判据：

1. 有效期的两条边界（0 与 168 小时）——两侧各验一次；
2. 窗口门限的下界（0 = 把这道闸关掉）；
3. 「存量任务里还留着旧键」这件事到底怎么认出来的。

⚠️ **一律断言具体数字，不写「等于那个常量」的自反断言**：后者改了常量照样绿，
等于什么都没守住（惯例来自 `tests/integration/application/test_behaviour_knobs.py`）。
"""

from __future__ import annotations

import pytest

from evo_helper.application.mission_scheduler import (
    _legacy_window_keys,
    _score_max_age_hours,
    _window_floor_value,
)
from evo_helper.domain.missions import MissionParamError
from evo_helper.domain.target_order import SCORE_MAX_AGE_MAX_HOURS

# -- 有效期 --------------------------------------------------------------------


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_max_age_means_follow_the_default(blank: object) -> None:
    """⚠️ **「没配」不是「配了 0」。**

    库里那一列的 NULL 表达的是「跟着代码默认走」（2 小时）。空串被读成 0 的话，
    它会撞上下界当场 400——而用户按下保存时的意思是「我不想管这一格」。
    """
    assert _score_max_age_hours(blank) is None


def test_a_max_age_of_zero_is_refused_because_it_inverts_the_knob() -> None:
    """填 0 的后果和填 0 的人想要的**正好相反**，所以当场拒掉。

    0 意味着「没有一条读数算新」→ 窗口内恒为 0 个 → 永远不足门限 → **每一轮都
    放弃窗口、改用全部旧读数**。也就是说这个旋钮被填成了它的反面：看起来是
    「只用最新数据」，实际是「一律拿旧读数打」，而页面上只会显示那句正常的
    「军力读数已放宽窗口」。默默接受 0 比拒掉糟得多。
    """
    with pytest.raises(MissionParamError):
        _score_max_age_hours(0)


@pytest.mark.parametrize("value", [-1, -0.5])
def test_a_negative_max_age_is_refused(value: object) -> None:
    """负数和 0 同一档：一条读数都不可能落进一个负宽度的窗口。"""
    with pytest.raises(MissionParamError):
        _score_max_age_hours(value)


def test_a_fractional_max_age_survives_intact() -> None:
    """⚠️ **1.5 小时必须原样活下来。**

    页面上这一格的步长一直是 0.5。这条判据若走 `_optional_int`（或库里那一列是
    `Integer`、或读侧走 `int()`），1.5 会变成 1——窗口窄了三分之一，而日志里写着
    1.0，看起来完全正常。这条用例存在的意义就是让任何一次「顺手取整」当场转红。
    """
    assert _score_max_age_hours(1.5) == 1.5
    assert _score_max_age_hours("2.5") == 2.5


def test_the_max_age_stops_at_one_week() -> None:
    """一周是上界，因为**再往上这个数挡不掉任何东西**。

    bot 军力每周一 UTC+0 刷新，而选靶第 2 步的周期边界已经把上周期的读数整批挡在
    外面了（`domain.target_order.reading_is_from_this_cycle`）。所以有效期超过一周
    之后，它筛不掉任何一条本周期还留得住的读数——而一个填了却什么都不做的旋钮比
    没有这个旋钮更坏：用户会以为自己已经把窗口调宽了，实际什么都没发生。
    """
    assert SCORE_MAX_AGE_MAX_HOURS == 168
    assert _score_max_age_hours(168) == 168
    with pytest.raises(MissionParamError):
        _score_max_age_hours(168.5)


@pytest.mark.parametrize("value", [True, False, "两小时", [], {}])
def test_a_max_age_that_is_not_a_number_is_refused(value: object) -> None:
    """`bool` 也要拒。

    它是 `int` 的子类，`True` 会被读成 1 小时——而用户敲进去的根本不是一个时长。
    JSON 里 `true` 完全可能是别处的开关被误送进这一格的。
    """
    with pytest.raises(MissionParamError):
        _score_max_age_hours(value)


# -- 窗口门限 ------------------------------------------------------------------


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_a_blank_window_floor_means_follow_the_default(blank: object) -> None:
    """同有效期：NULL 是「跟着代码默认走」（100 个），不是 0。"""
    assert _window_floor_value(blank) is None


def test_a_window_floor_of_zero_is_refused_because_it_silences_the_alarm() -> None:
    """填 0 会让**该响的告警一个字都不写**，所以当场拒掉。

    0 意味着「窗口内一个都不用有也算够」→ 窗口**永远不会被放弃**。听起来像是
    「更严格」，实际后果相反：窗口内真的一个都没有的夜里（周一凌晨正是如此），
    这一轮会在一个空池子上选靶、一发不派，而「军力读数已放宽窗口」那条 WARNING
    不会出现——**这道闸存在的全部意义就是别悄悄停摆**。
    """
    with pytest.raises(MissionParamError):
        _window_floor_value(0)


def test_a_window_floor_of_one_is_allowed() -> None:
    """1 是最激进的合法挡位：窗口内有一个就肯只信新数据。"""
    assert _window_floor_value(1) == 1


def test_a_big_window_floor_is_allowed_on_purpose() -> None:
    """⚠️ **上界刻意不设**，这条用例就是那句话的钉子。

    门限该多大取决于候选池此刻有多少个（实测库里 3000+ 个 bot，而窗口内能有多少
    又取决于军力榜扫描的节奏），写死一个上界就是拿一个凭空的数去卡用户真实的处境。
    填得比池子还大的后果是窗口每轮都被放弃，**而那件事会告警**——从日志里一眼看得
    出来，不需要一道拦在前面的墙。
    """
    assert _window_floor_value(100_000) == 100_000


@pytest.mark.parametrize("value", [1.5, True, "一百"])
def test_a_window_floor_that_is_not_a_whole_count_is_refused(value: object) -> None:
    """门限数的是「几个目标」，小数没有意义；`bool` 会被读成 1 个。"""
    with pytest.raises(MissionParamError):
        _window_floor_value(value)


# -- 存量任务里的旧键 ----------------------------------------------------------


def test_the_three_legacy_keys_are_all_recognised() -> None:
    """三个旧键一个都不能漏。

    `rescan_after_hours` 是有效期最早的名字、`score_max_age_hours` 是它改名之后
    搬家之前的名字、`top_n` 是窗口门限在 `params_json` 里的键。漏认一个的后果是
    那个任务顶着一个再也不生效的值，而**告警不会响**——用户看到的是「某个银河突然
    打得少了」，日志里一句解释都没有。
    """
    raw = '{"by_military": true, "top_n": 2, "score_max_age_hours": 6, "rescan_after_hours": 1}'

    assert _legacy_window_keys(raw) == {
        "top_n": 2,
        "score_max_age_hours": 6,
        "rescan_after_hours": 1,
    }


def test_a_task_without_the_legacy_keys_says_nothing() -> None:
    """没有旧键就一个字都不说——每轮都响的告警和不响的告警一样没用。

    这也是「在任务页保存一次告警就消失」那条善后能成立的地方：保存把那几个键
    从 `params_json` 里删掉，这里就返回空。
    """
    assert _legacy_window_keys('{"by_military": true, "max_score": 70000}') == {}


def test_a_legacy_value_equal_to_the_default_still_counts() -> None:
    """⚠️ **判据是「这个键在不在」，不是「它等不等于默认值」。**

    按取值判的话，「用户当年就配的 2 小时 / 100 个」会被当成没配过，于是那条任务
    **永远不告警**——而它同样存着一个再也不生效的值，用户同样需要知道这件事已经
    改成全局了。
    """
    assert _legacy_window_keys('{"by_military": true, "top_n": 100, "score_max_age_hours": 2}') == {
        "top_n": 100,
        "score_max_age_hours": 2,
    }


def test_an_illegal_legacy_value_is_reported_not_raised() -> None:
    """⚠️ **旧值不合法也只是被忽略，绝不抛异常。**

    抛出去会一路走到 `repository.disable_mission_task`：任务被自动停用、挂上
    `disabled_reason`，用户不去页面点一次「恢复」就永远不跑。而这个任务此刻**完全
    能正常派遣**——那个值已经不参与任何判据了，拿它把整条链路停掉是最坏的处置。
    """
    raw = '{"by_military": true, "top_n": -5, "rescan_after_hours": "六"}'

    assert _legacy_window_keys(raw) == {"top_n": -5, "rescan_after_hours": "六"}


def test_broken_params_json_is_not_reported_as_a_legacy_value() -> None:
    """`params_json` 整个读不出来时返回空，而不是炸在告警这条路上。

    这句话的载体是 `_params`（本仓一贯的解析入口）：坏 JSON 在别处已经有自己的
    善后，而「要不要为旧键告警」这件事没有资格成为它第二个失败点。
    """
    assert _legacy_window_keys("{ not json") == {}
