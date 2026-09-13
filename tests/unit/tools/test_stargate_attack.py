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
        flying: bool = False,
    ) -> None:
        self.quota = quota
        self.flying = flying
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

    def _stargate_already_flying(self) -> bool:
        return self.flying

    def _open_stargate_target(self) -> bool:
        return True

    def _stargate_quota(self) -> tuple[int, int] | None:
        return self.quota

    def _launch_stargate(self) -> int:
        self.launched += 1
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


class _FlyingProbe:
    """只为 `_stargate_already_flying` 准备的桩：把列表内容喂进去，看它怎么判。"""

    def __init__(self, rows: list[str]) -> None:
        self.rows = rows
        self.said: list[str] = []

    def _in_flight_destinations(self) -> list[str]:
        return self.rows

    class _Nav:
        def invalidate(self) -> None:
            pass

    class _Driver:
        def click(self, x: int, y: int, *, label: str = "") -> None:
            pass

        def wait(self, _seconds: float) -> None:
            pass

    _navigator = _Nav()
    _driver = _Driver()


def _flying_with(rows: list[str]) -> bool:
    import evo_helper.tools.pirate_loop as module

    probe = _FlyingProbe(rows)
    original = module.say
    module.say = probe.said.append
    try:
        return PirateLoop._stargate_already_flying(probe)  # type: ignore[arg-type]
    finally:
        module.say = original


class TestOneAtATime:
    def test_a_call_dispatches_at_most_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """⚠️ **本文件的重点。** 用户口径：「星门一次只能发出一发」。

        额度还剩 5 次、我们的上限是 3 —— 一个会循环的实现会在这里打三发，
        而那三发里只有第一发能飞出去，另外两发的下场我们并不知道。
        """
        loop = _Loop((5, 5))

        assert _run(loop, monkeypatch) == 1
        assert loop.launched == 1


class TestAskingTheGameHowManyAreLeft:
    @pytest.mark.parametrize(
        ("quota", "cap", "launched"),
        [
            ((5, 5), 3, 1),  # 一次没打过
            ((3, 5), 3, 1),  # 已用 2，还能打第 3 发
            ((2, 5), 3, 0),  # 已用 3 = 上限，收手（游戏那边还剩 2 次）
            ((0, 5), 3, 0),  # 游戏说没了
            ((5, 5), 0, 0),  # 上限 0 = 关掉
        ],
    )
    def test_used_is_total_minus_remaining(
        self, monkeypatch: pytest.MonkeyPatch, quota: tuple[int, int], cap: int, launched: int
    ) -> None:
        """⚠️ 已用 = 总数 − 剩余，**不自己记账**。

        `(2, 5)` 那一行是关键：游戏还肯让我们打，是**我们自己**收手。
        自己记一份计数的话，用户手动打过的那几发我们看不见，于是会超打。
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


class TestNotDispatchingWhileOneIsFlying:
    """⚠️ 「星门一次只能发出一发」这条口径**有两道闸**，各自守着不同的失效形态。"""

    def test_the_cheap_precheck_skips_the_whole_trip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """飞行中列表里有矮星系统就不走那条路 —— 省掉整整一趟点击。

        ⚠️ **每日次数那个读数答不了这个问题**：它数的是「今天派出过几发」，
        不是「现在有没有一发在飞」。2026-09-13 实机：09:50 派出一发，10:17 那趟
        读到「还剩 4/5」于是照打，被游戏当场挡下。
        """
        loop = _Loop((5, 5), flying=True)

        assert _run(loop, monkeypatch) == 0
        assert loop.launched == 0

    def test_nothing_in_flight_means_go(self, monkeypatch: pytest.MonkeyPatch) -> None:
        loop = _Loop((5, 5), flying=False)

        assert _run(loop, monkeypatch) == 1

    def test_an_unreadable_list_counts_as_flying(self) -> None:
        """⚠️⚠️ **这是这一轮实机最贵的一条。**

        第一版倒向「没在飞」，理由是「后面还有一道权威闸：真派时游戏自己会挡」。
        **那个前提是错的** —— 2026-09-13 实机拍到飞行中列表里同时有两发矮星系统
        （10:20 与 10:28 各一发），游戏根本没挡第二发。

        所以「一次只能发出一发」只能由我们自己保证：读不出时宁可不打。
        少打一发下一轮补得回来，多打一发收不回来。
        """
        assert _flying_with(["", "", "", ""]) is True, (
            "一行都读不出来时必须当成「有在飞」；当成「没在飞」会让这道闸永远放行。"
        )

    @pytest.mark.parametrize(
        ("rows", "expected"),
        [
            (["矮星系统", "神秘星云", "神秘星云", "神秘星云"], True),
            (["神秘星云", "神秘星云", "神秘星云", "矮星系统"], True),  # 在最后一行
            (["神秘星云", "神秘星云", "", ""], False),
            (["", "", "", ""], True),  # 一行都读不出 → 当成在飞
        ],
    )
    def test_any_readable_row_decides(self, rows: list[str], expected: bool) -> None:
        """只要读出了**一行**，就按读到的内容判；矮星系统出现在哪一行都算。"""
        assert _flying_with(rows) is expected

    def test_the_list_is_read_one_row_at_a_time(self) -> None:
        """⚠️ 取字函数恒用 `--psm 7`（单行），整块读**实测恒为空字符串**。

        而空字符串的意思正好是「列表里没有矮星系统」—— 这道闸于是永远放行。
        2026-09-13 实机就是这么放过去一发的。
        """
        source = inspect.getsource(PirateLoop._in_flight_destinations)

        assert "IN_FLIGHT_ROW_PITCH" in source, "必须一行一读，不能框住整张列表"
        assert "IN_FLIGHT_VISIBLE_ROWS" in source


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
