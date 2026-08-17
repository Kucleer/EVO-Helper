"""对账翻信箱的时间下限：攻击配置页上那个框。

## 为什么有这个参数

用户口径（2026-08-17）：活动期间信箱里堆着**大几百封**活动战报排在最上面，而库里
最近一封攻击战报停在好几天前（PR #158 那两天战报链路断了），于是「撞见库里已有的
那一封就收工」这条早停迟迟不触发，对账那一趟把翻页预算整个烧满。

> 可能我的希望是，不要读那么多，毕竟数量是大几百封
> 这个参数改为可配置，这样遇到活动我可以灵活调整

## 默认为什么是 6 小时——**不是性能优化**

对账那一趟的活是把**还在等的**那几发的战报读回来，而「还在等」本身就以 6 小时为
界：`due_attack_dispatches` / `bot_dispatch_facts` 都按 `MAX_REPORT_AGE` 把更早的
派遣剔掉，`storage.intel.RESULT_NO_REPORT` 在那一刻就把它们判成「战报永远不会来
了」。再往下翻，翻到的都是没有任何一条判据还在等的战报。

⚠️ 这**不等于**「6 小时以上的战报认领不上」——认领窗口是
`dispatched_at_utc >= reported_at - MAX_REPORT_AGE`，相对战报自己的时间戳算，
隔多久读回来都认领得上。所以救历史战报**确实**做得到，只不过那是 `--exhaustive`
手动补录的活，而补录不受这个下限约束（那一条钉在
`tests/unit/tools/test_backfill_reports.py`）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import evo_helper.web
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.missions import MissionParamError
from evo_helper.domain.report_wait import DEFAULT_REPORT_SCAN_FLOOR, REPORT_SCAN_HOURS_MAX
from evo_helper.storage.repository import SqlAlchemyRepository

from .conftest import Clock, make_supervisor


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 8, 17, 12, 0, tzinfo=UTC))


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


# -- 常量本身 ------------------------------------------------------------------


def test_the_default_floor_is_still_six_hours() -> None:
    """⚠️ **断言具体数字，不是「等于那个常量」。**

    写成 `assert DEFAULT_REPORT_SCAN_FLOOR == DEFAULT_REPORT_SCAN_FLOOR` 那样的
    自反断言，把默认值改成 0 小时（＝下界就是此刻，一封都翻不到）用例照样绿。
    """
    assert DEFAULT_REPORT_SCAN_FLOOR.total_seconds() == 6 * 3600


def test_the_ceiling_is_a_typo_guard_not_a_policy_line() -> None:
    """上界给到 30 天：它拦的是手滑与 `timedelta` 溢出，不是「配多大才有意义」。

    「超过 6 小时之后多读回来的战报没人还在等」那条留在页面上提示，不硬拦——
    用户可能有别的用途。
    """
    assert REPORT_SCAN_HOURS_MAX == 24 * 30


# -- 收得下的取值 --------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_a_blank_box_means_follow_the_default(scheduler: MissionScheduler, raw: object) -> None:
    """留空不是错，是「跟着默认走」。返回 `None` 让读侧去回落——默认值只该有一处。"""
    assert scheduler.validate_report_scan_hours(raw) is None


@pytest.mark.parametrize("raw", [1, 3, 6, 72, REPORT_SCAN_HOURS_MAX])
def test_a_positive_hour_count_is_taken_as_is(scheduler: MissionScheduler, raw: int) -> None:
    assert scheduler.validate_report_scan_hours(raw) == raw


def test_a_value_above_six_hours_is_allowed_with_only_a_note_on_the_page() -> None:
    """⚠️ 超过 6 小时**不拦**。用户口径是「灵活调整」，硬拦等于替他做决定。"""
    assert REPORT_SCAN_HOURS_MAX > 6


# -- 拒掉不可能的取值 ----------------------------------------------------------


def test_zero_is_refused_because_it_would_read_nothing_at_all(
    scheduler: MissionScheduler,
) -> None:
    """⚠️ **这里的 0 和「盲拖屏数」那个 0 相反，必须拒。**

    盲拖的 0 是最保守的一侧；这里的 0 意味着下界就是「此刻」，而信箱里每一封都
    比此刻旧——对账那一趟一封都翻不到，还一声不响。留空才是「跟着默认走」。
    """
    with pytest.raises(MissionParamError):
        scheduler.validate_report_scan_hours(0)


@pytest.mark.parametrize("raw", [-1, -6, REPORT_SCAN_HOURS_MAX + 1, 10**9])
def test_out_of_range_values_are_refused(scheduler: MissionScheduler, raw: int) -> None:
    """负数没有意义；大到离谱的那一侧会让 `now - timedelta(...)` 直接溢出。"""
    with pytest.raises(MissionParamError):
        scheduler.validate_report_scan_hours(raw)


@pytest.mark.parametrize("raw", [2.5, "六小时", True, [6]])
def test_non_integer_values_are_refused(scheduler: MissionScheduler, raw: object) -> None:
    """`True` 也要拒：`bool` 是 `int` 的子类，放过去就成了「往回读 1 小时」。"""
    with pytest.raises(MissionParamError):
        scheduler.validate_report_scan_hours(raw)


# -- 存得住 --------------------------------------------------------------------


def test_the_value_survives_a_round_trip_through_the_global_attack_config(
    repository: SqlAlchemyRepository,
) -> None:
    """存在既有的全局攻击配置表里，和档位、盲拖屏数同一行、同一次原子替换。"""
    repository.replace_military_attack_tiers("[]", blind_scrolls=12, report_scan_hours=3)

    row = repository.military_attack_config()

    assert row.report_scan_hours == 3
    assert row.blind_scrolls == 12, "整份替换不能把同一行上的另一项冲掉"


def test_a_fresh_row_starts_out_blank_so_upgrades_change_nothing(
    scheduler: MissionScheduler, repository: SqlAlchemyRepository
) -> None:
    """`prepare()` 建出来的那一行是 NULL＝留空＝跟着默认走。

    列上刻意没有 `server_default`：给了默认值，既有那一行会被钉死在当时的 6 上，
    日后调默认值它不跟。这条也是「升级完成那一刻行为完全不变」的保证。
    """
    assert repository.military_attack_config().report_scan_hours is None


# -- 页面上说得出这件事 --------------------------------------------------------


def test_the_attack_settings_page_carries_the_box_and_both_directions_of_risk() -> None:
    """框要在**攻击配置页**上（用户指定的位置），而且两个方向的代价都要写清。

    用户要这个参数就是为了活动期间灵活调整，所以「调小会怎样、调大会怎样」必须
    并排摆着；只写一半会让人往一个方向调到底。还得写明救历史战报该走补录——
    否则用户会把这个数调得很大，而那是绕远路。
    """
    page = (Path(evo_helper.web.__file__).parent / "templates" / "settings.html").read_text(
        encoding="utf-8"
    )

    assert 'id="report-scan-hours"' in page, "攻击配置页上没有这个输入框，等于功能不存在"
    assert "调小" in page and "调大" in page, "两个方向的代价要并排摆着"
    assert "翻不到" in page, "调小的代价：比这个时间还旧的战报翻不到"
    assert "手动补录" in page and "不受这个下限约束" in page, "要指明救历史战报的正路"
