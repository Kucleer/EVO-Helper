"""星门打矮星系统：判据与账。

⚠️ 这里守的是**三条口径**（用户 2026-09-13）：

1. 「星门一次只能发出一发」 → `attack_stargate` **不循环**；
2. 「每日只需要攻击 3 次」 → 上限是我们自己的，而**已用几次问游戏**
   （面板上写着 `剩余/总数`，已用 = 总数 − 剩余）；
3. 「不占用航线」 → 这条路**一行 `attack_dispatches` 都不写**。

⚠️ 第 2 条那个算法是这一版最要紧的设计：自己记一份当日计数，跨重启、跨用户手动打、
跨日重置都会和真相分家 —— 那正是这个仓库里「配额算错」那一类故障的老根。
"""

from __future__ import annotations

import inspect

import pytest

from evo_helper.tools import pirate_loop
from evo_helper.tools.pirate_loop import STARGATE_QUOTA_RE, PirateLoop


class _Loop:
    """只装上这几条用例够得着的那几件东西，不起真的循环。"""

    #: 默认上限跟着生产那份走：用例不该自己发明一个数，那样改了常量也不会红。
    STARGATE_DAILY_ATTACKS = PirateLoop.STARGATE_DAILY_ATTACKS

    def __init__(
        self,
        quota: tuple[int, int] | None,
        *,
        cap: int | None = None,
        decrements: bool = True,
    ) -> None:
        self.quota = quota
        #: 真游戏**在派出那一刻就扣**这个数（2026-09-13 实测：10:20 派出后 4/5 → 3/5）。
        #: 桩默认照做 —— 不照做的话这张表测的就不是循环条件，而是那道硬上界。
        self.decrements = decrements
        self.opened = 0
        self.launched = 0
        self.left = 0
        self.clicks: list[str] = []
        self.said: list[str] = []
        if cap is not None:
            self.STARGATE_DAILY_ATTACKS = cap

    # -- 被 attack_stargate 调到的那几个 --------------------------------------
    def _open_for_stargate(self) -> None:
        self.opened += 1

    def _open_stargate_target(self) -> bool:
        return True

    def _stargate_quota(self) -> tuple[int, int] | None:
        return self.quota

    #: ⚠️ 桩自己的熔断。没有它，「循环少了硬上界」这个 bug 的表现是**测试挂死**
    #: 而不是变红 —— 挂死的用例在 CI 上只会超时，没人看得出是哪一条出的问题。
    MAX_LAUNCHES = 10

    def _launch_stargate(self) -> int:
        self.launched += 1
        if self.launched > self.MAX_LAUNCHES:
            raise AssertionError(
                f"派了 {self.launched} 发还没停 —— 循环没有硬上界，超打是收不回来的"
            )
        if self.decrements and self.quota is not None:
            remaining, total = self.quota
            self.quota = (max(0, remaining - 1), total)
        return 1

    def _leave_stargate(self) -> None:
        self.left += 1

    class _Driver:
        def __init__(self, outer: _Loop) -> None:
            self.outer = outer

        def click(self, x: int, y: int, *, label: str = "") -> None:
            self.outer.clicks.append(label)

        def wait(self, _seconds: float) -> None:
            pass

    @property
    def _driver(self) -> _Loop._Driver:
        return _Loop._Driver(self)


def _run(loop: _Loop, monkeypatch: pytest.MonkeyPatch, **kwargs: object) -> int:
    monkeypatch.setattr(pirate_loop, "say", loop.said.append)
    monkeypatch.setattr(pirate_loop, "record_system_log", lambda *a, **k: None)
    return PirateLoop.attack_stargate(loop, **kwargs)  # type: ignore[arg-type]


class TestBurstingTheWholeDailyShare:
    """⚠️ **口径当天改过一次，改的依据是实拍。**

    先说的是「星门一次只能发出一发」，而飞行中列表里拍到**同时有两发**矮星系统 ——
    游戏不拦。用户据此改口径：「直接在 4 系，一发把 3 轮都打掉」。
    """

    def test_one_trip_fires_the_whole_daily_share(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """额度满、上限 3 → 一趟打三发。"""
        loop = _Loop((5, 5))

        assert _run(loop, monkeypatch, daily_cap=3) == 3
        assert loop.launched == 3

    def test_the_loop_is_hard_capped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """⚠️ **循环次数硬性封顶，不能只靠「重读配额」当出口。**

        这里的桩让配额**永远不动**（模拟游戏改了扣减时机、或者读数漂了）。
        没有那道上界就会一直打下去 —— 而超打是收不回来的。
        """
        loop = _Loop((5, 5), decrements=False)

        assert _run(loop, monkeypatch, daily_cap=2) == 2
        assert loop.launched == 2


class TestAskingTheGameHowManyAreLeft:
    @pytest.mark.parametrize(
        ("quota", "cap", "launched"),
        [
            ((5, 5), 3, 3),  # 一次没打过 → 一趟打满三发
            ((4, 5), 3, 2),  # 已用 1 → 还能打两发
            ((3, 5), 3, 1),  # 已用 2 → 只剩第 3 发
            ((2, 5), 3, 0),  # 已用 3 = 上限，收手（游戏那边还剩 2 次）
            ((1, 5), 3, 0),  # 已用 4（用户自己手动打过）→ 照样收手
            ((0, 5), 3, 0),  # 游戏说没了
            ((5, 5), 0, 0),  # 上限 0 = 关掉
        ],
    )
    def test_used_is_total_minus_remaining(
        self, monkeypatch: pytest.MonkeyPatch, quota: tuple[int, int], cap: int, launched: int
    ) -> None:
        """⚠️ 已用 = 总数 − 剩余，**不自己记账**。

        `(2, 5)` 与 `(1, 5)` 那两行是关键：游戏还肯让我们打，是**我们自己**收手 ——
        而 `(1, 5)` 那一行只可能来自用户手动打过。自己记一份计数的话那几发我们看不见，
        于是会超打。
        """
        loop = _Loop(quota)

        assert _run(loop, monkeypatch, daily_cap=cap) == launched
        assert loop.launched == launched

    def test_an_unreadable_quota_never_guesses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """读不出就不打。**不许兜底成「大概还能打」** —— 猜错就是超打。"""
        loop = _Loop(None)

        assert _run(loop, monkeypatch) == 0
        assert loop.launched == 0
        assert loop.left == 1, "读不出也要退出面板，不能把游戏停在那一屏上"

    def test_the_two_refusals_are_said_apart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """「游戏不让打了」和「我们自己收着不打」必须是两句话。

        混成一句，日后想调高上限时没人知道该调哪儿。
        """
        game_says_no = _Loop((0, 5))
        _run(game_says_no, monkeypatch, daily_cap=3)
        ours = _Loop((2, 5))
        _run(ours, monkeypatch, daily_cap=3)

        assert any("用完" in line for line in game_says_no.said)
        assert any("我们自己" in line for line in ours.said)


class TestTheQuotaRegex:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [("5/5", ("5", "5")), ("4 / 5", ("4", "5")), ("剩余 3/5 次", ("3", "5"))],
    )
    def test_it_reads_remaining_and_total(self, text: str, expected: tuple[str, str]) -> None:
        match = STARGATE_QUOTA_RE.search(text)
        assert match is not None
        assert match.groups() == expected

    def test_it_does_not_match_a_bare_number(self) -> None:
        """⚠️ 光有一个数不算数：`剩余/总数` 两个都要，否则算不出「已用」。"""
        assert STARGATE_QUOTA_RE.search("5") is None


class TestNotTouchingTheDispatchLedger:
    def test_the_stargate_path_writes_no_attack_dispatch_row(self) -> None:
        """⚠️ 用户口径：「不占用航线」。

        `attack_dispatches` 的每个消费者都默认「一行 = 占一条航线 + 用掉一次当日
        攻击额度」。这条路写进去就得让航线账、日配额、选靶窗口、周期统计每一处
        判据都学会这个新发次 —— 而「没学会」正是这个仓库里反复咬人的那一类 bug
        （#312 / #320 / #321 / #323 四次同一个形状）。

        所以这里钉的是一个**「不做」**：星门那几个方法里不许出现落账的调用。
        """
        forbidden = ("save_dispatch", "_record_dispatch", "save_attack_intent", "_record_intent")
        for name in (
            "attack_stargate",
            "_open_stargate_target",
            "_launch_stargate",
            "_ensure_stargate_planet",
        ):
            source = inspect.getsource(getattr(PirateLoop, name))
            for call in forbidden:
                assert call not in source, (
                    f"{name} 里出现了 {call}：星门不占航线、不吃当日攻击额度，"
                    "写进 attack_dispatches 会让航线账与配额一起算错。"
                )


class TestTheOpeningSteps:
    def test_it_does_the_same_opening_as_a_normal_round(self) -> None:
        """⚠️ **这一条是实机打回来的**（2026-09-13）。

        第一版直接从 `_goto_planet_surface` 开始，而那时游戏停在登录页上，
        于是整趟在「切不回星球地表」上停住、一发没派。少的是 `_ensure_session`：
        它才是把游戏从登录页/读条页带进游戏内的那一步。
        """
        source = inspect.getsource(PirateLoop._open_for_stargate)

        assert "ensure_game_window" in source, "窗口尺寸被改过时所有坐标一起失效"
        assert "_ensure_session" in source, "少了它就会从登录页上开始点"
        assert "_reset_to_known_screen" in source


class TestTheLaunchIsNotAssumedToHaveWorked:
    def test_the_dialog_check_sits_after_the_go_button(self) -> None:
        """⚠️ **点完「出发！」不等于派出去了。**

        2026-09-13 实机：上一发还在飞时游戏把这一发挡下来，而第一版这里直接就报了
        「已派出」—— 日志上凭空多出一发，配额账跟着错。常规 `attack()` 那条路早就
        写着同一句话，这条路当时漏抄了。

        这里钉的是**顺序**：弹窗检查必须排在点「出发」之后、记日志之前。
        """
        source = inspect.getsource(PirateLoop._launch_stargate)
        go = source.index("BRIEFING_LAUNCH_BUTTON")
        dialog = source.index("_handle_dialog")
        logged = source.index("record_system_log")

        assert go < dialog < logged, (
            "顺序必须是「点出发 → 问弹窗 → 才记日志」；记在问之前就会把被挡下的那一发记成已派出。"
        )
