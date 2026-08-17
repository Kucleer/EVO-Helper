"""环境故障该拿哪个退出码收场。

`EXIT_ENVIRONMENT_BUSY`（75）的语义是「环境暂时不行、会自己好，不算失败」——
`MissionExit.failed` 看到 75 直接返回 False，一次豁免都不消耗。可本该报 75 的条件
过去全写成了硬失败 1，于是 2026-08-17 凌晨那一阵环境故障里，三条链路各自撞上、
**每一轮都吃掉一次**「多条一起倒」的豁免，26 分钟就把 6/6 的上限攒满了；再往下
就会把失败算到各个任务头上，连续够了任务被自动停用。按 75 收场的话一次都不会消耗。

**这个文件钉的是那条分界线，两个方向都要钉。** 判错任何一侧都有代价，而且不对称：

- 该报 1 的报成了 75 → **静默死循环**：不计故障、不报警，停顿看门狗也接不住
  （它抓的是「跑着却没进展」，而这种情形每轮几十秒就干净退出），任务在页面上
  整夜显示「在跑」，实际一件事都不做。
- 该报 75 的报成了 1 → 只是多攒几次失败计数，最坏是任务被自动停用并报警。

所以分界线是：

1. **抢不到前台 → 无条件 75。** 那一条什么都不做（不关窗、不重开、一次点击都不发），
   纯粹让路等用户不再用别的窗口。
2. **会话回不到游戏内 → 看关窗重开配额。** 走到那里说明这一轮已经关过窗、
   重开过 Chrome 并且失败了；配额是滚动窗口内有限的，所以「还有配额就报 75」
   这条判据**必然有尽头**。配额耗尽还是回不去就退回 1。
3. **其余一律 1**：维护类、配置类、以及任何「不确定会不会自己好」的条件。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

import pytest

from evo_helper.application.mission_supervisor import MissionExit, StopReason
from evo_helper.domain.scheduler import (
    EXIT_ENVIRONMENT_BUSY,
    MissionKind,
    exit_code_for_environment_fault,
)
from evo_helper.game.game_window import ForegroundUnavailable, GameWindowError
from evo_helper.game.session_keeper import ReconnectOutcome, ScreenState
from evo_helper.tools import ranking_scan, scan_coordinates
from evo_helper.tools.pirate_loop import Outcome, exit_code_for
from evo_helper.tools.scan_coordinates import (
    LiveDriver,
    exit_code_for_unusable_session,
    run_with_foreground_guard,
)

NOW = datetime(2026, 8, 17, 2, 0, tzinfo=UTC)


def _exit(code: int | None, *, stopped_by: StopReason = StopReason.SELF) -> MissionExit:
    return MissionExit(
        task_id=1,
        kind=MissionKind.PIRATE,
        command=(),
        exit_code=code,
        stopped_by=stopped_by,
        started_at_utc=NOW,
        ended_at_utc=NOW,
    )


# -- 判据本身 ------------------------------------------------------------------


def test_the_two_directions_are_the_only_two_answers() -> None:
    """一个有名字的分岔口，好过散在各处的 `75 if ... else 1`。"""
    assert exit_code_for_environment_fault(recoverable=True) == EXIT_ENVIRONMENT_BUSY
    assert exit_code_for_environment_fault(recoverable=False) == 1


def test_the_busy_code_costs_no_exemption_while_one_costs_one() -> None:
    """整套机制的落点：75 不算失败，1 算。**这一条红了，上面全部白改。**"""
    assert not _exit(EXIT_ENVIRONMENT_BUSY).failed
    assert _exit(1).failed


# -- 第 1 类：抢不到前台，无条件 75 ---------------------------------------------


class _Driver(LiveDriver):
    """只借 `focus()`。**不跑父类 `__init__`**：那里会去加载 pyautogui。"""

    def __init__(self, handle: int) -> None:
        self._handle = handle
        self.raised = 0

    def window(self) -> object:
        return type("W", (), {"handle": self._handle})()

    def _raise_to_front(self, handle: int) -> None:
        self.raised += 1


class _Win32Gui:
    """假的 `win32gui`，只回答「此刻谁在前台」。全程不碰真窗口。"""

    def __init__(self, foreground: int) -> None:
        self._foreground = foreground

    def GetForegroundWindow(self) -> int:  # noqa: N802 - 照抄 win32 的名字
        return self._foreground


def test_losing_the_foreground_race_raises_the_dedicated_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """裸 `RuntimeError` 全仓一处都没 catch，抛穿 `main()` 就是退出码 1。"""
    monkeypatch.setitem(sys.modules, "win32gui", _Win32Gui(foreground=999))
    driver = _Driver(handle=1)

    with pytest.raises(ForegroundUnavailable):
        driver.focus(attempts=1)

    assert driver.raised == 1, "抛之前该退避重试，不是一读到就放弃"


def test_a_round_that_never_got_the_foreground_ends_as_environment_busy() -> None:
    """**无条件 75，不看任何配额。**

    这一条和「会话回不来」正相反：它一次点击都没发、一个窗口都没关，纯粹在让路。
    用户放开鼠标、切走那个窗口，下一轮就好——这正是 75 那一档被设计出来时
    唯一的服务对象。
    """

    def body() -> int:
        raise ForegroundUnavailable("游戏窗口抢不到前台")

    assert run_with_foreground_guard(body) == EXIT_ENVIRONMENT_BUSY


def test_a_clean_round_passes_its_own_exit_code_through() -> None:
    """守卫不许改写正常收场的退出码。"""
    assert run_with_foreground_guard(lambda: 0) == 0
    assert run_with_foreground_guard(lambda: 2) == 2


def test_other_window_failures_are_not_excused(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **只放行「抢不到前台」这一个类型。**

    `GameWindowError` 的其余成员（窗口拉不起来、尺寸调不到标定值）不会因为用户
    放开鼠标就好；豁免它们就是把整夜空转合法化。
    """

    def body() -> int:
        raise GameWindowError("窗口尺寸调不到标定值")

    with pytest.raises(GameWindowError):
        run_with_foreground_guard(body)


# -- 第 2、3 类：会话回不到游戏内，看关窗重开配额 --------------------------------


def _unusable(restarts_left: int) -> ReconnectOutcome:
    return ReconnectOutcome(
        ScreenState.UNKNOWN,
        reconnected=False,
        detail="unrecognised screen",
        restarts_left=restarts_left,
    )


def test_a_session_with_restart_budget_left_is_worth_another_round() -> None:
    """还有配额 = 恢复阶梯还没走到头，下一轮再试有意义，别吃豁免。"""
    assert exit_code_for_unusable_session(_unusable(restarts_left=2)) == EXIT_ENVIRONMENT_BUSY


def test_an_exhausted_restart_budget_is_a_real_failure() -> None:
    """⚠️ **这条是整个改动的安全底线。**

    配额耗尽还是回不去，说明关窗重开这条路已经证明救不了——这不是暂时的。
    此时若照样报 75，调度器每隔一个冷却就再起一轮、再走一遍
    `ensure_connected(force=True)`、再什么都不推进，而**豁免计数不再增长，
    再没有任何东西会最终把它停下来**。2026-08-17 那种故障就会从
    「26 分钟后被 6/6 拦住」变成整夜静默空转。
    """
    assert exit_code_for_unusable_session(_unusable(restarts_left=0)) == 1


def test_a_missing_verdict_is_read_as_a_real_failure() -> None:
    """判据读不出来时倒向 1 那一侧：判错成 75 的代价大得多。"""
    assert exit_code_for_unusable_session(None) == 1


# -- 第 2 类的落点：榜单采集 -----------------------------------------------------


class _Keeper:
    """假的 `SessionKeeper`。剧本用完就一直给最后那个结局。"""

    def __init__(self, outcomes: list[ReconnectOutcome]) -> None:
        self._outcomes = outcomes
        self.restarts: list[str] = []

    def ensure_connected(self, *, force: bool = False) -> ReconnectOutcome:
        return self._outcomes[0] if len(self._outcomes) == 1 else self._outcomes.pop(0)

    def restart_and_reenter(self, reason: str) -> ReconnectOutcome:
        self.restarts.append(reason)
        return self._outcomes[0] if len(self._outcomes) == 1 else self._outcomes.pop(0)


class _NoopDriver:
    def click(self, _x: int, _y: int, *, label: str = "") -> None:
        pass

    def wait(self, _seconds: float) -> None:
        pass


def _ranking_entry(monkeypatch: pytest.MonkeyPatch, outcome: ReconnectOutcome) -> int:
    keeper = _Keeper([outcome])
    monkeypatch.setattr(scan_coordinates, "make_ocr", lambda: object())
    monkeypatch.setattr(scan_coordinates, "make_session_keeper", lambda *_a, **_k: keeper)
    monkeypatch.setattr(scan_coordinates, "say", lambda _m: None)
    monkeypatch.setattr(ranking_scan, "say", lambda _m: None)
    driver = _NoopDriver()
    return ranking_scan.enter_game_exit_code(driver, object())  # type: ignore[arg-type]


def test_the_ranking_run_lets_a_recoverable_screen_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """`if not ensure_in_game(...): return 1` 是这次修掉的三处之一。"""
    assert _ranking_entry(monkeypatch, _unusable(restarts_left=1)) == EXIT_ENVIRONMENT_BUSY


def test_the_ranking_run_still_fails_when_the_budget_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _ranking_entry(monkeypatch, _unusable(restarts_left=0)) == 1


def test_a_ranking_run_that_gets_into_the_game_reports_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ready = ReconnectOutcome(ScreenState.IN_GAME, reconnected=True, detail="", restarts_left=0)

    assert _ranking_entry(monkeypatch, ready) == 0


def test_the_ranking_run_walks_the_whole_recovery_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **阶梯要走全，否则「还有配额」这条判据自己就没有尽头。**

    只巡检一次就收的话，每一轮都在配额满格的状态下报 75——那正是上面那条底线
    要防的静默空转。走全阶梯还顺手治了 `UNKNOWN` 最常见的成因（浮层压着导航条）。
    """
    keeper = _Keeper([_unusable(restarts_left=0)])
    monkeypatch.setattr(scan_coordinates, "make_ocr", lambda: object())
    monkeypatch.setattr(scan_coordinates, "make_session_keeper", lambda *_a, **_k: keeper)
    monkeypatch.setattr(scan_coordinates, "say", lambda _m: None)
    monkeypatch.setattr(ranking_scan, "say", lambda _m: None)

    ranking_scan.enter_game_exit_code(_NoopDriver(), object())  # type: ignore[arg-type]

    assert len(keeper.restarts) == 1, "关窗重开那一级必须走到，配额才说明得了问题"


# -- 明确不放行的那些 -----------------------------------------------------------


def test_a_mailbox_that_stays_unreachable_is_still_a_hard_failure() -> None:
    """维护/配置这一类**不许**挪进 75。

    单子非空却翻不了信箱、升级重启之后还是翻不了，那几发的 6 小时钟正在走，
    再丢一轮就永久判缺失。它需要有人来管，而 75 恰恰是「没人管也会好」。
    """
    assert exit_code_for(Outcome(failed="开工翻不了信箱")) == 1


def test_a_misconfigured_origin_planet_is_still_a_hard_failure() -> None:
    """列表里根本没这颗星球 = `origin` 配错了，它永远不会自己好。

    豁免它的后果就是那个静默死循环：每轮 30 秒就退，不计故障、不报警，
    任务整夜显示「在跑」而一发不派。
    """
    assert exit_code_for(Outcome(busy="切不到出发星球", busy_is_permanent=True)) == 1
    # 反面：回读没认出来那一档是画面状态问题，下一轮多半就好。
    assert exit_code_for(Outcome(busy="切到出发星球之后回读没认出来")) == EXIT_ENVIRONMENT_BUSY
