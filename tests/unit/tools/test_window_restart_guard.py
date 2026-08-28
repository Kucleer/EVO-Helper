"""「确保窗口在」这一步也要落进关窗重开的保护圈里。

## 生产实况（2026-08-28 昨夜 00:00–01:15）

游戏窗口没了（Chrome 挂掉 / 机器休眠 / 游戏进程死了）。六个任务（1 个 RANKING +
5 个 BOT）每一轮起来**约 1 秒就死**，`exit=1`，库里每轮只留下一行：

    00:25:28.622  [__main__] 模式：真打；目标 1:453:7=CCC, ...
                             （完，pid=23032，之后一个字都没有）

整个故障时段 `game.session_keeper` **一行日志都没写**——也就是说，昨夜没有任何
东西尝试重开过游戏窗口，一直到早上人工介入。

原因是层次错了：会重开 Chrome 的 `restart_and_reenter` 在 `BotLoop.run()` **内部**
调，而进程死在它上面一行的 `driver.window()`。那套自愈机制的前提是「窗口已经在、
只是走岔了」，窗口本身没了的时候它够不着。

## 这个文件钉的是**分档**，不是「重开一定救得回来」

- 「窗口不见了」（`GameWindowMissing`）→ 重开一次再试。
- 「抢不到前台」（`ForegroundUnavailable`）→ **行为逐字不变**，一次重开都不许发生。
  那一档的语义是「什么都不做、纯粹让路」，重开反而会去抢用户的窗口。
- 「几何/配置不对」（其余 `GameWindowError`）→ 不重开，直接失败。新窗口照样调不到
  标定视口、照样读同一份配置，重开只是白关一次用户的窗口。
- 配额是 `SessionKeeper` 那一份，不是新开的计数器——用尽就不再重开。

⚠️ 全程不碰真窗口：`driver` 是假的，重开动作在 `SessionKeeper` 里本来就是注入的。
"""

from __future__ import annotations

import ctypes
from typing import Any

import pytest

from evo_helper.domain.scheduler import EXIT_ENVIRONMENT_BUSY
from evo_helper.game.game_window import (
    ForegroundUnavailable,
    GameWindowError,
    GameWindowMissing,
)
from evo_helper.game.session_keeper import (
    ReconnectOutcome,
    ScreenState,
    SessionKeeper,
)
from evo_helper.tools import scan_coordinates
from evo_helper.tools.scan_coordinates import (
    WINDOW_GUARD_VERSION,
    ensure_window_or_restart,
    run_with_foreground_guard,
)

WINDOW = object()


class _Driver:
    """按剧本回答 `window()`：剧本里是异常就抛，否则原样交回。

    剧本用完就一直给最后那一项——「第二次也失败」和「一直失败」在这里是同一件事。
    """

    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self.calls = 0

    def window(self) -> Any:
        self.calls += 1
        step = self._script[min(self.calls - 1, len(self._script) - 1)]
        if isinstance(step, BaseException):
            raise step
        return step


class _Keeper:
    """假的会话守护。只回答重开这一件事，并记下被调了几次、理由是什么。"""

    def __init__(self, outcome: ReconnectOutcome, *, left_before: int = 3) -> None:
        self._outcome = outcome
        self._left_before = left_before
        self.reasons: list[str] = []

    def restarts_left(self) -> int:
        return self._left_before if not self.reasons else self._outcome.restarts_left

    def restart_and_reenter(self, reason: str) -> ReconnectOutcome:
        self.reasons.append(reason)
        return self._outcome

    def ensure_connected(self, *, force: bool = False) -> ReconnectOutcome:
        """巡检永远说「会话好好的」。本文件钉的是**窗口**那一步，不是会话那一步。"""
        return _ready()


def _ready(restarts_left: int = 2) -> ReconnectOutcome:
    return ReconnectOutcome(
        ScreenState.IN_GAME,
        reconnected=True,
        detail="restarted the game window and re-entered the session",
        restarts_left=restarts_left,
    )


def _refused(
    detail: str = "restart budget exhausted: 3/3 restarts within 3600s",
    *,
    restarts_left: int = 0,
) -> ReconnectOutcome:
    return ReconnectOutcome(
        ScreenState.UNKNOWN, reconnected=False, detail=detail, restarts_left=restarts_left
    )


class _Recorder:
    """接住 `record_system_log`。实机在另一台机器上，库里这几条就是全部证据。"""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, dict[str, Any]]] = []

    def __call__(self, level: str, source: str, message: str, *, payload: Any = None) -> None:  # noqa: D102
        self.rows.append((level, source, message, dict(payload or {})))

    @property
    def messages(self) -> list[str]:
        return [row[2] for row in self.rows]

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return [row[3] for row in self.rows]


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    sink = _Recorder()
    monkeypatch.setattr(scan_coordinates, "record_system_log", sink)
    monkeypatch.setattr(scan_coordinates, "say", lambda _message: None)
    return sink


# -- 第 1 档：窗口不见了，重开能救 ----------------------------------------------


def test_a_window_that_comes_back_after_one_restart_lets_the_round_go_on(
    recorder: _Recorder,
) -> None:
    """昨夜那一档：第一次拿不到窗口 → 重开一次 → 第二次拿到了 → 这一轮照常跑。

    **重开只许一次。** 做成循环的话，服务端维护 / Chrome 起不来的时候就成了
    「关一次 Chrome、开一次、再关」一直折腾到有人来看——`MAX_WINDOW_RESTARTS`
    整段讲的就是它。
    """
    driver = _Driver([GameWindowMissing("拉起游戏后 120s 内没等到窗口出现"), WINDOW])
    keeper = _Keeper(_ready())

    def body() -> int:
        assert ensure_window_or_restart(driver, keeper, chain="tools.bot_loop") is WINDOW
        return 0

    assert run_with_foreground_guard(body) == 0
    assert len(keeper.reasons) == 1, "重开只许一次，不许循环"
    assert driver.calls == 2, "重开之后要再确认一次窗口，不能假定它回来了"


def test_the_restart_that_saved_the_round_says_so_in_the_database(
    recorder: _Recorder,
) -> None:
    """实机在另一台机器上：**只靠库里的 `system_log` 就要说得清发生过什么。**"""
    driver = _Driver([GameWindowMissing("拉起游戏后 120s 内没等到窗口出现"), WINDOW])
    keeper = _Keeper(_ready(restarts_left=2), left_before=3)

    ensure_window_or_restart(driver, keeper, chain="tools.bot_loop")

    payload = recorder.payloads[-1]
    assert payload["restart_attempted"] is True, "重开被触发了吗"
    assert payload["restarts_left_before"] == 3, "第几次——重开前后的余额都要记"
    assert payload["restarts_left_after"] == 2
    assert payload["restart_ready"] is True, "成功没有"
    assert payload["first_error_type"] == "GameWindowMissing", "被什么逼到要重开的"
    assert "120s" in payload["first_error"], "原文照抄，不要只留结论"


# -- 第 2 档：重开也救不回来 ----------------------------------------------------


def test_a_restart_that_did_not_help_is_told_apart_from_the_first_failure(
    recorder: _Recorder,
) -> None:
    """⚠️ 「重开失败」和「窗口第一次就没起来」**在库里不许长得一样**。

    昨夜九轮之所以查不动，正是因为两者在日志上都是「什么都没有」。分不开就没法
    回答「重开这条路到底试没试过」——而那是决定要不要去机房的那一个问题。
    """
    driver = _Driver([GameWindowMissing("拉起游戏后 120s 内没等到窗口出现")])
    keeper = _Keeper(_refused())

    with pytest.raises(GameWindowError) as failure:
        ensure_window_or_restart(driver, keeper, chain="tools.bot_loop")

    assert "重开" in str(failure.value), "异常本身就要说得出是重开没救回来"
    assert len(keeper.reasons) == 1
    payload = recorder.payloads[-1]
    assert payload["restart_attempted"] is True
    assert payload["restart_ready"] is False
    assert payload["restart_detail"] == "restart budget exhausted: 3/3 restarts within 3600s", (
        "失败卡在哪一步，照抄 `ReconnectOutcome.detail`"
    )

    # 反面：顺利那一轮写进库的是另一句话、另一个 `outcome`。分不开就白记了。
    ensure_window_or_restart(_Driver([WINDOW]), _Keeper(_ready()), chain="tools.bot_loop")
    assert recorder.messages[-1] != recorder.messages[-2]
    assert recorder.payloads[-1]["outcome"] != payload["outcome"]


def test_the_round_ends_non_zero_when_the_restart_could_not_bring_the_window_back(
    recorder: _Recorder,
) -> None:
    """重开也救不了就按失败收场——**不许挪进 `EXIT_ENVIRONMENT_BUSY`**。

    那一档的准入条件是「什么都不做也会自己好」。走到这里说明这一轮已经关过窗、
    重开过 Chrome，窗口还是拉不起来：那是 Chrome 挂了 / 机器休眠 / profile 锁着
    这一类，等一轮不会好，需要有人去看。豁免它就是把整夜静默空转合法化。
    """
    driver = _Driver([GameWindowMissing("拉起游戏后 120s 内没等到窗口出现")])
    keeper = _Keeper(_refused())

    def body() -> int:
        ensure_window_or_restart(driver, keeper, chain="tools.bot_loop")
        return 0

    with pytest.raises(GameWindowError):
        run_with_foreground_guard(body)


def test_a_window_that_is_still_missing_after_a_successful_re_entry_stops_too(
    recorder: _Recorder,
) -> None:
    """重开报告「已经回到游戏内」，但窗口仍然拿不到——照样停，且说得出停在哪一步。"""
    driver = _Driver([GameWindowMissing("拉起游戏后 120s 内没等到窗口出现")])
    keeper = _Keeper(_ready())

    with pytest.raises(GameWindowError):
        ensure_window_or_restart(driver, keeper, chain="tools.bot_loop")

    assert len(keeper.reasons) == 1, "第二次也拿不到窗口时不许再重开一次"
    assert recorder.payloads[-1]["outcome"] == "window_still_missing_after_restart"


# -- 第 3 档：抢不到前台，行为逐字不变 ------------------------------------------


def test_losing_the_foreground_race_never_triggers_a_restart(recorder: _Recorder) -> None:
    """⚠️ **这一条最要紧。**

    「抢不到前台」的语义是**什么都不做**：不关窗、不重开、一次点击都不发，纯粹让路
    等用户不再用别的窗口（`game.game_window.ForegroundUnavailable` 整段讲的就是它）。
    在这一档上重开，等于趁用户正在用别的窗口时把游戏窗口关掉再抢一次前台——
    比什么都不做糟得多。
    """
    driver = _Driver([ForegroundUnavailable("游戏窗口抢不到前台")])
    keeper = _Keeper(_ready())

    def body() -> int:
        ensure_window_or_restart(driver, keeper, chain="tools.bot_loop")
        return 0

    assert run_with_foreground_guard(body) == EXIT_ENVIRONMENT_BUSY, "退出码逐字不变"
    assert keeper.reasons == [], "一次重开都不许发生"
    assert driver.calls == 1, "也不许重试一次窗口"


# -- 第 4 档：重开救不了的那些，不重开 ------------------------------------------


@pytest.mark.parametrize(
    ("failure", "why"),
    [
        (
            GameWindowError("窗口视口是 1920x877，标定值是 1920x879；几何不符时拒绝继续采集"),
            "几何调不到标定值是标定问题，新窗口照样调不到",
        ),
        (
            GameWindowError("页面 DPR 配成了 1.0，标定值是 1.25"),
            "配置问题，重开走的是同一份配置——而且 `ensure_game_window` 第一句就再校验一次",
        ),
        (
            GameWindowError("找不到 Chrome；无法拉起游戏窗口"),
            "重开也要 `chrome_path()`，同一处、同一个结论",
        ),
        (
            GameWindowError("有 2 个标题为 'EVO' 的窗口，无法判断用哪个"),
            "`restart_game_window` 第一句 `find()` 就抛同一个错，且它明说要人工关窗口",
        ),
    ],
)
def test_failures_a_restart_cannot_fix_are_not_restarted(
    recorder: _Recorder, failure: GameWindowError, why: str
) -> None:
    """重开不是万能钥匙：救不了的那一档，重开只是白关一次用户的窗口。"""
    driver = _Driver([failure])
    keeper = _Keeper(_ready())

    with pytest.raises(GameWindowError):
        ensure_window_or_restart(driver, keeper, chain="tools.bot_loop")

    assert keeper.reasons == [], why
    assert driver.calls == 1


# -- 配额：用的是 `SessionKeeper` 那一份，不是新开的计数器 ----------------------


def _real_keeper(restarts: list[str], *, max_restarts: int = 1) -> SessionKeeper:
    """真的 `SessionKeeper`，只把「关窗重开」这个动作换成记一笔。

    ⚠️ 用真的而不是假的，是因为这一条钉的正是「配额是它那一份」：假 keeper 只能
    证明我们调了它，证明不了计数器没有被重开第二份。
    """

    def restart_window() -> None:
        restarts.append("restarted")

    return SessionKeeper(
        observe=lambda: ScreenState.IN_GAME,
        click_entry=lambda: None,
        click_start=lambda: None,
        restart_window=restart_window,
        max_restarts=max_restarts,
        sleep=lambda _seconds: None,
    )


def test_the_restart_budget_is_the_session_keeper_s_own(recorder: _Recorder) -> None:
    """配额用尽之后就不再重开——**上限只有一份**。

    另开一个计数器等于把上限翻倍，正是 `MAX_WINDOW_RESTARTS` 那段注释要防的：
    服务端维护时每一轮都撞这一屏，没有真正的上限就成了一直关一次开一次
    折腾到有人来看。
    """
    restarts: list[str] = []
    keeper = _real_keeper(restarts, max_restarts=1)
    missing = GameWindowMissing("拉起游戏后 120s 内没等到窗口出现")

    # 第一次：配额够，重开一次就救回来了。
    assert (
        ensure_window_or_restart(_Driver([missing, WINDOW]), keeper, chain="tools.bot_loop")
        is WINDOW
    )
    assert restarts == ["restarted"]

    # 第二次：同一个 keeper，配额已经用完——不再关窗口，直接失败。
    with pytest.raises(GameWindowError):
        ensure_window_or_restart(_Driver([missing, WINDOW]), keeper, chain="tools.bot_loop")
    assert restarts == ["restarted"], "配额用尽之后一次都不许再关窗口"
    assert recorder.payloads[-1]["restart_ready"] is False
    assert "budget" in recorder.payloads[-1]["restart_detail"]


# -- 版本指纹 ------------------------------------------------------------------


def test_every_round_leaves_a_fingerprint_only_this_version_can_write(
    recorder: _Recorder,
) -> None:
    """⚠️ **库里要认得出生产跑的是哪一版。**

    仓库刚为此吃过亏：改动上线之后没有任何一个键是只有新代码写得出的，于是
    「生产到底跑没跑这一版」只能靠猜。这一条钉的就是那个键——它在**顺利那一轮**
    也要写，否则一个平安夜过后什么都分辨不出来。
    """
    ensure_window_or_restart(_Driver([WINDOW]), _Keeper(_ready()), chain="tools.bot_loop")

    payload = recorder.payloads[-1]
    assert payload["guard_version"] == WINDOW_GUARD_VERSION
    assert payload["outcome"] == "window_already_up"
    assert payload["restart_attempted"] is False


def test_the_log_line_says_which_runner_it_came_from(recorder: _Recorder) -> None:
    """`source` 就是调用方交进来的那条链路名，一个字都不加工。

    六个任务同时倒下时，「是哪一条链路」是第一个要回答的问题。
    """
    ensure_window_or_restart(_Driver([WINDOW]), _Keeper(_ready()), chain="tools.ranking_scan")

    assert recorder.rows[-1][1] == "tools.ranking_scan"


def test_the_chain_is_named_by_the_caller_not_sniffed_off_the_stack(
    recorder: _Recorder,
) -> None:
    """⚠️⚠️ **链路名必须由调用方传字面量，不许从调用栈上取模块名。**

    第一版走的是 `_caller_source()`（`sys._getframe(2).f_globals["__name__"]`）。
    单元测试里它一直是对的，**而生产上一上线就写错**——2026-08-28 14:40:12 那一条
    是 `source='__main__'`，三条链路一模一样，设计意图整个落空。

    差别在**调用方是谁**：四个调用点都在各自模块的 `main()` 里，而那几个模块是
    **当脚本跑的**，`__name__` 就是 `"__main__"`。栈上取模块名这招只对「被 import
    的模块里的函数」成立，对入口本身恰恰不成立——而这里全都是入口。

    ⚠️ 上一版的用例**接不住这个错**，而且不是写漏了：它断言 `source` 以
    `test_window_restart_guard` 结尾，在测试里恒真——因为测试模块是被 import 的。
    **测试环境与生产环境在这一点上语义相反**，所以那条用例越绿越说明不了问题。
    这一条改成钉「交进去什么就记什么」，与调用方叫什么名字无关。
    """
    ensure_window_or_restart(_Driver([WINDOW]), _Keeper(_ready()), chain="随便什么字符串")

    source = recorder.rows[-1][1]
    assert source == "随便什么字符串"
    assert source != "__main__", "又从栈上取模块名了"
    assert "test_window_restart_guard" not in source, "还是在猜调用方是谁"


def test_every_entry_point_names_a_different_chain() -> None:
    """⚠️ 四个入口各报各的名字，**互不相同**。

    全都报同一个名字的话，这个参数就白加了——那正是 `__main__` 那一版的症状：
    键在、值没用。这一条直接读源码里的调用点，因为「四个入口都填对了」这件事
    没法靠跑一遍来证明（跑起来要真窗口）。
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "src" / "evo_helper" / "tools"
    found: dict[str, str] = {}
    for name in ("bot_loop", "pirate_loop", "ranking_scan", "scan_coordinates"):
        text = (root / f"{name}.py").read_text(encoding="utf-8")
        calls = re.findall(r'ensure_window_or_restart\([^)]*chain="([^"]+)"', text)
        assert len(calls) == 1, f"{name} 里的调用点不是一处，是 {len(calls)} 处"
        found[name] = calls[0]

    assert len(set(found.values())) == 4, f"有入口报了重名：{found}"
    assert "__main__" not in set(found.values())


# -- 三个入口都接上了 -----------------------------------------------------------


class _Windll:
    """假的 `ctypes.windll`。`main()` 第一句就要声明 DPI 感知，Linux 上没有它。"""

    class _Shcore:
        @staticmethod
        def SetProcessDpiAwareness(_level: int) -> None:  # noqa: N802 - 照抄 win32 的名字
            return None

    shcore = _Shcore()


class _Loop:
    """假的 `BotLoop` / `PirateLoop`：只回答「这一轮跑完了」。"""

    built: list[_Loop] = []

    def __init__(self, *_args: Any, session_keeper: Any = None, **_kwargs: Any) -> None:
        self.session_keeper = session_keeper
        _Loop.built.append(self)

    def run(self) -> Any:
        from evo_helper.tools.pirate_loop import Outcome

        return Outcome()


@pytest.fixture
def entry_point(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> Any:
    """把三个入口共用的那几样换成假的，只留下「窗口 → 重开 → 接着跑」这条线。"""
    monkeypatch.setattr(ctypes, "windll", _Windll(), raising=False)
    _Loop.built = []

    def wire(module: Any, loop_attr: str | None) -> _Keeper:
        keeper = _Keeper(_ready())
        driver = _Driver([GameWindowMissing("拉起游戏后 120s 内没等到窗口出现"), WINDOW])
        monkeypatch.setattr(module, "install_runner_system_log", lambda: None, raising=False)
        monkeypatch.setattr(module, "LiveDriver", lambda **_kwargs: driver)
        monkeypatch.setattr(module, "make_ocr", lambda: object(), raising=False)
        monkeypatch.setattr(module, "say", lambda _message: None)
        # 入口点在 import 时就把这个名字绑到自己模块里了，所以要按模块打桩。
        monkeypatch.setattr(module, "make_session_keeper", _spy(keeper))
        if loop_attr is not None:
            monkeypatch.setattr(module, loop_attr, _Loop)
        return keeper

    return wire


def _spy(keeper: _Keeper) -> Any:
    def build(*_args: Any, **_kwargs: Any) -> _Keeper:
        build.calls += 1  # type: ignore[attr-defined]
        return keeper

    build.calls = 0  # type: ignore[attr-defined]
    return build


def test_the_bot_runner_recovers_a_missing_window(entry_point: Any) -> None:
    """5 个 BOT 任务里的那一条。"""
    from evo_helper.tools import bot_loop

    keeper = entry_point(bot_loop, "BotLoop")

    assert bot_loop.main(["--targets", "1:453:7=CCC", "--attack"]) == 0
    assert len(keeper.reasons) == 1, "窗口不见了要重开一次"
    assert _Loop.built[0].session_keeper is keeper, "整轮共用同一个守护，否则重开配额就有了第二份"


def test_the_pirate_runner_recovers_a_missing_window(entry_point: Any) -> None:
    from evo_helper.tools import pirate_loop

    keeper = entry_point(pirate_loop, "PirateLoop")

    assert pirate_loop.main(["--systems", "1:453"]) == 0
    assert len(keeper.reasons) == 1
    assert _Loop.built[0].session_keeper is keeper


def test_the_ranking_runner_recovers_a_missing_window(
    monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    """RANKING 那一条。它建 keeper 的地方是 `enter_game_exit_code`。"""
    from evo_helper.tools import ranking_scan

    keeper = _Keeper(_ready())
    driver = _Driver([GameWindowMissing("拉起游戏后 120s 内没等到窗口出现"), WINDOW])
    monkeypatch.setattr(scan_coordinates, "make_ocr", lambda: object())
    monkeypatch.setattr(scan_coordinates, "make_session_keeper", lambda *_a, **_k: keeper)
    monkeypatch.setattr(ranking_scan, "say", lambda _message: None)

    assert ranking_scan.enter_game_exit_code(driver, object()) == 0  # type: ignore[arg-type]
    assert len(keeper.reasons) == 1


def test_the_guard_runs_before_anything_touches_the_screen(entry_point: Any) -> None:
    """⚠️ 顺序：**先确保窗口在，再建循环**。

    反过来的话，循环的构造函数一碰画面就会抛在保护圈外面——那正是昨夜的形状。
    """
    from evo_helper.tools import bot_loop

    keeper = entry_point(bot_loop, "BotLoop")
    bot_loop.main(["--targets", "1:453:7=CCC"])

    assert keeper.reasons, "重开发生在循环建起来之前"
    assert _Loop.built, "重开成功之后这一轮要接着跑，不是就地收工"


def test_the_loop_uses_the_keeper_the_entry_point_already_spent_budget_on() -> None:
    """⚠️ **整轮只许有一份关窗重开配额。**

    开工那一步（`ensure_window_or_restart`）可能已经花掉一次配额，而配额住在
    `SessionKeeper` 实例里。循环要是自己再建一个，`MAX_WINDOW_RESTARTS` 就凭空
    翻倍——「另开一份计数就等于把上限翻倍」这句话在 `_restart_now` 上写着，
    而它防的正是「服务端维护时一直关一次开一次折腾到有人来看」。

    这一条走**真的** `BotLoop`：入口点那条用例用的是假循环，证明不了
    `BotLoop.__init__` 有没有把守护转交给父类。
    """
    from evo_helper.tools.bot_loop import BotLoop, BotOptions

    keeper = _Keeper(_ready())
    loop = BotLoop(object(), object(), BotOptions(targets=(), attack=False), session_keeper=keeper)  # type: ignore[arg-type]

    assert loop._keeper() is keeper  # noqa: SLF001 - 钉的就是这份内部状态


def test_a_loop_built_without_one_still_makes_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """不传仍旧惰性自建——手工调子方法和老测试都走这条路。"""
    from evo_helper.tools import scan_coordinates as module
    from evo_helper.tools.bot_loop import BotLoop, BotOptions

    made = _Keeper(_ready())
    monkeypatch.setattr(module, "make_session_keeper", lambda *_a, **_k: made)
    loop = BotLoop(object(), object(), BotOptions(targets=(), attack=False))  # type: ignore[arg-type]

    assert loop._keeper() is made  # noqa: SLF001 - 同上


def test_naming_the_chain_is_not_optional() -> None:
    """⚠️ `chain` **必填**，不许给默认值。

    给了默认值，新加一条链路时漏填就不再是 `TypeError`，而是又一条说谎的日志 ——
    正是 `__main__` 那一版的症状：键在、值没用、而且要等上了生产才看得出来。
    这一条钉的是「漏填会当场炸」，因为那是这个参数唯一比自动嗅探强的地方。
    """
    import inspect

    from evo_helper.tools.scan_coordinates import ensure_window_or_restart

    chain = inspect.signature(ensure_window_or_restart).parameters["chain"]
    assert chain.kind is inspect.Parameter.KEYWORD_ONLY, (
        "必须是关键字参数，位置传容易和 keeper 串位"
    )
    assert chain.default is inspect.Parameter.empty, "给了默认值，漏填就不会炸了"
