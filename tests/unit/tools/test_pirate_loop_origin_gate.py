"""**每一发派遣之前**都要回读「起点」，确认脚底下还是这一轮配的那颗星球。

## 事故（生产，2026-08-18）

调度器决定 `--origin 9:250:8`，库里也按 9:250:8 记账，**同一轮的第二发却是从
主星 4:277:15 打出去的**。飞行时间是硬证据：

    18:53:32 → 9:231:7    实际 18.5 分 | 从 9:250:8 应 18.6 分（误差 0.5%）
    18:56:22 → 9:205:14   实际 125.0 分 | 从 4:277:15 应 125.0 分（误差 0%）

而两发之间日志里**一条切星球记录都没有**：

    18:51:47  出发星球：切到 9:250:8
    18:52:07    起点回读 '9:250:8'，确认当前星球是 9:250:8
    18:53:32    已发动攻击 → 9:231:7（预设 AAA）
    18:56:22    已发动攻击 → 9:205:14（预设 BBB）     ← 这一发已经是 4:277:15

游戏自己把当前星球退回了主星，而 runner 不知道。

代价有两层。一发白占 **3.4 小时**航线（往返 45 分钟 → 250 分钟）；更贵的是
**账是错的**——#179 那两道航线闸按 9:250:8 扣，实际占的是 4:277:15 的额度，
多出发点的整套航线记账在算假账。

## 缺陷

`game.planet_list` 的规矩是「点『前往此处』→ 开派遣面板回读『起点』确认真的换了」，
但**那个回读只在切换那一刻做一次**，之后每一发都假定脚底下没变过。

同形的教训仓库里早有：`game.system_navigator.SystemNavigator` 就是因为「打完字
不等于跳过去了」才改成**只信回读确认过的坐标**。出发星球这一层缺的正是这条——
切换那一次是「记下来」，本文件钉的是「每次用之前再问一遍」。

## 第二次事故（生产，2026-08-27）：读不出**未必**是漂了

4 系一天起了 8 轮，**7 轮死在同一个目标 `4:268:5` 上**，每轮都是这个形状：

    08:43:18  出发星球：切到 4:277:15
    08:43:44    起点回读 '4:277:15'，确认当前星球是 4:277:15
    08:43:51    导航栏回读 ('4','277','15')，确认停在 4:277:15
    08:44:14    派遣面板起点回读 （读不出）（原文 ''）
    08:44:15    起点核对不过；这一发没派，这一轮到此为止

存下来的现场图（`system_log` id 109168）上，那一刻屏幕上是**保护期弹窗**
（「没有可执行的任务。」）盖住了面板 —— 所以起点那一格自然读空。星球明明切对了，
上面两行刚确认过。

而 `OriginDrifted` 是 `RoundExhausted` 的子类，抛出去就是整轮结束，于是它**赶在
`_handle_dialog` 之前**把这一轮杀掉了。后果是个自维持的循环：`_note_protection_period`
没跑到 → `protection_seen_at_utc` 一直是 `None` → 目标没被排除、军力 10580 又离得近
→ 下一轮又被挑中 → 又撞同一个弹窗。次数一路在涨：08-25 四次 → 08-26 六次 →
08-27 十四次。

⇒ 读不出时**先把屏交给 `_handle_dialog` 按弹窗类型认**。⚠️ 只在「读不出」那一档问，
读出来是**别的坐标**时不问 —— 那是真漂了，弹窗解释不了它。

## 本文件钉住的五条

1. 起点与期望一致 → 照常派（攻击、侦察各一条）。
2. 不一致 → 不派、记 `refused`、留现场、写 `system_log`、停下这一轮。
3. **读不出要重读；重读完仍读不出、且屏上没有认得的弹窗，按「核不过」收场。**
4. **读不出且屏上是保护期弹窗 → 记保护期、跳过这个目标、这一轮继续。**
5. 简报任务类型那道既有闸门不受影响——两道闸门各拦各的，谁也别顶替谁。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.game import pirate_ui
from evo_helper.tools import pirate_loop as module
from evo_helper.tools.pirate_loop import (
    ORIGIN_SETTLE_TRIES,
    LoopOptions,
    OriginDrifted,
    Outcome,
    PirateLoop,
    RoundExhausted,
)

#: 这一轮配的出发星球（= 会写进 `attack_intents.origin_*` 的那一个）。
ORIGIN = Coordinate(9, 250, 8)
#: 游戏偷偷退回去的那颗主星。
MAIN_PLANET = Coordinate(4, 277, 15)
TARGET = Coordinate(9, 205, 14)


class _Driver:
    """记下每一次点击的标签，顺序原样保留。`wait` 不真的睡。"""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def click(self, _x: int, _y: int, *, label: str = "") -> None:
        self.events.append(f"click:{label}")

    def wait(self, _seconds: float) -> None:
        return None


class _Navigator:
    def __init__(self) -> None:
        self.invalidated = 0

    def ensure_system_view(self, _read_labels: Any) -> bool:
        return True

    def invalidate(self) -> None:
        self.invalidated += 1


class _Repository:
    def __init__(self) -> None:
        self.intents: list[Any] = []
        self.dispatches: list[Any] = []
        #: `_note_protection_period` 落进 `bot_targets.protection_seen_at_utc` 的那一笔。
        #: 2026-08-27 的循环就是因为这一笔一直没写成。
        self.protections: list[Coordinate] = []

    def note_protection_period(self, coordinate: Coordinate, *, seen_at_utc: Any) -> bool:
        del seen_at_utc
        self.protections.append(coordinate)
        return True

    def military_attack_config(self) -> Any:
        return SimpleNamespace(protection_exclusion_hours=None)

    def save_attack_intent(self, intent: Any) -> None:
        self.intents.append(intent)

    def save_dispatch(self, dispatch: Any) -> None:
        self.dispatches.append(dispatch)

    def record_flight_time(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    origin_readings: list[str],
    briefing: str = "攻击",
    dialog: str = "",
    current_planet: Coordinate | None = ORIGIN,
    configured_origin: Coordinate = ORIGIN,
) -> Any:
    """一个能跑完 `attack()` / `scout()` 的 loop，只有「起点读到什么」是可编排的。

    `origin_readings` 按顺序喂给 `_read_coord_line`；用完就一直重复最后一条。
    编成清单而不是一个定值，是因为第 3 条（读不出要重读）钉的正是**跨帧**的行为：
    第一帧空、第二帧读得出，必须当成读得出。
    """
    events: list[str] = []

    class _Picker:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        def pick(self, name: str) -> None:
            events.append(f"pick:{name}")

    monkeypatch.setattr(module, "PresetPicker", _Picker)

    def _read(roi: tuple[int, int, int, int], **_kwargs: Any) -> str:
        if roi == pirate_ui.BRIEFING_FLIGHT_ROI:
            return "8分3秒"
        if roi == pirate_ui.BRIEFING_MISSION_ROI:
            return briefing
        if roi == pirate_ui.DIALOG_TEXT_ROI:
            return dialog
        return ""

    readings = list(origin_readings)

    def _read_coord_line(roi: tuple[int, int, int, int], _upscale: int, _resample: str) -> str:
        assert roi == pirate_ui.FLEET_ORIGIN_ROI, "起点闸门读错了框"
        events.append("read:起点")
        return readings.pop(0) if len(readings) > 1 else readings[0]

    loop = PirateLoop.__new__(PirateLoop)
    loop._driver = _Driver(events)
    loop._navigator = _Navigator()
    loop._options = LoopOptions(systems=(), scout=True, attack=True, origin=configured_origin)
    loop._outcome = Outcome()
    loop._repository = _Repository()
    # 给一个真 run_id，`_ensure_run` 才会短路到上面那个假仓库而不是去连库。
    loop._run_id = uuid4()
    loop._current_planet = current_planet
    loop._origin_dumps = 0
    loop._read = _read
    loop._read_coord_line = _read_coord_line
    loop.dumped = []
    loop._dump_frame = lambda name, roi=None: loop.dumped.append(name)
    loop._leave_dispatch_list = lambda: events.append("离开飞行中列表")
    loop._record_intent = lambda _coordinate, preset=None: None
    loop._record_dispatch = lambda *_args, **_kwargs: None
    loop.events = events
    return loop


# -- (a) 起点与期望一致 → 照常派 ---------------------------------------------


def test_a_matching_origin_lets_the_attack_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """脚底下就是这一轮配的那颗 → 照常派，闸门一步都不挡。"""
    loop = _loop(monkeypatch, origin_readings=[str(ORIGIN)])

    assert loop.attack(TARGET, preset="AAA") is True
    assert "click:出发" in loop.events
    assert loop._outcome.refused == []


def test_a_matching_origin_lets_the_scout_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """侦察一样要过这道闸门——它一样占航线、一样按出发坐标记账。"""
    loop = _loop(monkeypatch, origin_readings=[str(ORIGIN)], briefing="侦察")

    assert loop.scout(TARGET) is True
    assert "click:出发" in loop.events


def test_the_gate_reads_before_the_preset_strip_is_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**顺序是硬要求。**

    `PRESET_TOGGLE` 就坐在起点那一行的右端，预设条一展开，「预设 N/10」那一栏
    整个把起点盖住（实拍 `var/logs/atk-2-presets.png`）。挪到选完预设之后再读，
    读到的是预设名，于是闸门要么恒不过（每一发都拦）、要么被迫放宽——两条都是错的。
    """
    loop = _loop(monkeypatch, origin_readings=[str(ORIGIN)])

    loop.attack(TARGET, preset="AAA")

    assert loop.events.index("read:起点") < loop.events.index("pick:AAA")


# -- (b) 不一致 → 不派、记 refused、留现场 ------------------------------------


def test_a_drifted_origin_refuses_the_shot(monkeypatch: pytest.MonkeyPatch) -> None:
    """**本文件的重点。** 读到主星就不许照打——照打是把舰队送错地方、账还记错。"""
    loop = _loop(monkeypatch, origin_readings=[str(MAIN_PLANET)])

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")

    assert "click:确认终点" not in loop.events, "起点核不过还去点绿✓"
    assert "click:出发" not in loop.events, "起点核不过还把舰队派了出去"
    assert "pick:AAA" not in loop.events, "核不过的那一发不该再去翻预设条"


def test_a_drifted_origin_refuses_the_scout_too(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = _loop(monkeypatch, origin_readings=[str(MAIN_PLANET)], briefing="侦察")

    with pytest.raises(OriginDrifted):
        loop.scout(TARGET)

    assert "click:确认终点" not in loop.events
    assert "click:出发" not in loop.events


def test_the_refusal_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """拦下来要留一笔 `refused`，否则它和「这一位没有海盗」在日志上一模一样。"""
    loop = _loop(monkeypatch, origin_readings=[str(MAIN_PLANET)])

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")

    assert [coordinate for coordinate, _reason in loop._outcome.refused] == [TARGET]
    assert str(MAIN_PLANET) in loop._outcome.refused[0][1]


def test_a_drifted_origin_leaves_a_frame_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """只有一行文字复盘不了「那一刻画面上到底是什么」。

    ROI 读成 `4:277:15` 与「ROI 框歪了、读到别处的一串数字」在文字日志上长得
    一模一样——这张图是唯一能把两者分开的东西。
    """
    loop = _loop(monkeypatch, origin_readings=[str(MAIN_PLANET)])

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")

    assert loop.dumped == ["origin-mismatch"]


def test_the_frame_dumps_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """封顶，理由同 `_dump_coord_mismatch`：前几张就够定位了。"""
    loop = _loop(monkeypatch, origin_readings=[str(MAIN_PLANET)])

    for _attempt in range(PirateLoop.MAX_ORIGIN_DUMPS + 2):
        with pytest.raises(OriginDrifted):
            loop.attack(TARGET, preset="AAA")

    assert len(loop.dumped) == PirateLoop.MAX_ORIGIN_DUMPS


def test_the_mismatch_reaches_the_system_log(monkeypatch: pytest.MonkeyPatch) -> None:
    """**实机跑在另一台机器上**，`var/logs` 里那张图本机取不到。

    所以「期望哪颗星、读到哪颗星、原文是什么」必须落库；只写文件等于没写。
    """
    loop = _loop(monkeypatch, origin_readings=["4:277:15"])
    written: list[tuple[str, str, dict[str, Any]]] = []
    monkeypatch.setattr(
        module,
        "record_system_log",
        lambda level, source, message, payload=None: written.append(
            (level, message, dict(payload or {}))
        ),
    )

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")

    assert len(written) == 1
    level, message, payload = written[0]
    assert level == "WARNING"
    assert str(ORIGIN) in message and str(MAIN_PLANET) in message
    assert payload["expected_origin"] == str(ORIGIN)
    assert payload["origin_seen"] == str(MAIN_PLANET)
    assert payload["origin_raw_text"] == "4:277:15"
    assert payload["target"] == str(TARGET)


def test_the_round_stops_rather_than_grinding_through_every_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起点是**整轮共用**的状态，一发对不上，余下每一发都会对不上。

    链路里没有任何一步会在两个目标之间把星球切回来（`ensure_origin_planet`
    一轮只跑一次）。照着往下走的结果是几十次开关派遣面板、几十行一模一样的
    日志、零发派出。所以停下这一轮，让调度器起下一轮重新切一次。

    **而且必须以 `RoundExhausted` 的形式停**：那一档退出码 0、不计连续失败、
    不自动停用（用户口径「不停用、不记失败」）。当成失败的话，连撞三次就把
    整条链路停了，而它只需要下一轮重切一次星球。
    """
    assert issubclass(OriginDrifted, RoundExhausted)


def test_the_panel_is_closed_and_the_nav_cache_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """走之前把派遣面板关掉；面板开过之后导航栏里是什么已经不可知了。

    与 `attack()` 里「找不到预设」那一支同理——那一处漏掉 `invalidate()` 换来的是
    实机上最贵的一次故障（连续 44 个目标坐标核对全不过，13 分钟一发没派）。
    """
    loop = _loop(monkeypatch, origin_readings=[str(MAIN_PLANET)])

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")

    assert "click:关闭派遣面板" in loop.events
    assert loop._navigator.invalidated == 1


def test_the_disproved_memory_is_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    """「本轮已经切到哪」这份记忆刚被证伪，留着它只会让下一次 `switch_needed`
    说「不用切」。"""
    loop = _loop(monkeypatch, origin_readings=[str(MAIN_PLANET)])

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")

    assert loop._current_planet is None


def test_the_expectation_comes_from_the_ledger_not_from_the_runners_own_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**期望值必须是 `_options.origin`，不是 `_current_planet`。**

    要守的恰恰是记账：`_record_intent` 往 `attack_intents.origin_*` 写的就是
    `_options.origin or origin()`。拿 runner 自己那份记忆去比，比的是
    「我以为我在哪」对「我以为我在哪」——同义反复，正是这次事故里失效的那半边。

    这里把两者摆成对立：记忆说主星、配置说 9:250:8、面板读到主星。
    照记忆比会放行（而那一发会被按 9:250:8 记账），照配置比才拦得下。
    """
    loop = _loop(
        monkeypatch,
        origin_readings=[str(MAIN_PLANET)],
        current_planet=MAIN_PLANET,
        configured_origin=ORIGIN,
    )

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")


# -- (c) 读不出 → 重读；仍读不出按核不过收场 ----------------------------------


def test_an_unreadable_origin_is_reread_before_giving_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会动的画面上单帧的空结果是抛硬币，不是证据。

    与 `vision.scan_reading.read_panel_confirming`、
    `game.preset_picker.read_names_confirming` 同一条规矩。派遣面板同样是滑进来的。
    """
    loop = _loop(monkeypatch, origin_readings=[""])

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")

    # 每一轮都要把 `FLEET_ORIGIN_RECIPES` 两套配方都试过（`_fleet_origin_text`）。
    expected = ORIGIN_SETTLE_TRIES * len(pirate_ui.FLEET_ORIGIN_RECIPES)
    assert loop.events.count("read:起点") == expected


def test_a_late_frame_still_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    """重读不是走过场：第一帧空、第二帧读得出，就该照常派。

    没有这一条，「读不出按核不过收场」会被写成「一读空就停轮」，
    而面板首帧读空太常见——那样整条链路会被一次动画抖动拖停。

    编排成「两轮全空、第三轮才读出来」，钉的是**跨轮**重读：只在同一轮里换配方
    是不够的，面板铺开要等的是时间，不是换个放大倍数。
    """
    empty_rounds = 2 * len(pirate_ui.FLEET_ORIGIN_RECIPES)
    loop = _loop(monkeypatch, origin_readings=[""] * empty_rounds + [str(ORIGIN)])

    assert loop.attack(TARGET, preset="AAA") is True
    assert "click:出发" in loop.events
    assert loop.events.count("read:起点") > len(pirate_ui.FLEET_ORIGIN_RECIPES), (
        "只试了一轮就读出来了，这条用例没有钉到跨轮重读"
    )


def test_an_unreadable_origin_is_never_treated_as_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**读不出 ≠ 对上了。** 重读完还是读不出，按核不过收场。

    方向只能是这一个：漏判的代价是这一轮白等，误判的代价是整轮的台账都在撒谎
    （与 `domain.planet_switch.origin_confirmed` 一致）。
    """
    loop = _loop(monkeypatch, origin_readings=[""])

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")

    assert "click:出发" not in loop.events
    assert loop.dumped == ["origin-mismatch"]
    assert "读不出" in loop._outcome.refused[0][1]


def test_noise_that_is_not_a_coordinate_counts_as_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """「起点：」的尾巴被数字白名单压成零星数字时，挑不出三段数字 = 读不出。

    这是 `origin_in` 那条「前缀噪声凑不出三段」的另一面：它挑不出一个假坐标来，
    但也不许因此蒙混过关。
    """
    loop = _loop(monkeypatch, origin_readings=["1 7 5"])

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")


# -- (d) 既有的任务类型闸门不受影响 -------------------------------------------


def test_the_mission_gate_still_refuses_a_wrong_briefing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两道闸门各拦各的。起点对得上，简报写的不是攻击 → 仍旧走既有那条路。

    ⚠️ 它**不能**变成 `OriginDrifted`：简报写错是这一发的事（返回 False、跳到
    下一个目标），起点漂了才是整轮的事。混成一档，一次简报读串就把整轮停掉。
    """
    loop = _loop(monkeypatch, origin_readings=[str(ORIGIN)], briefing="探索")

    assert loop.attack(TARGET, preset="AAA") is False
    assert loop._outcome.refused == [(TARGET, "简报不是攻击")]
    assert "click:出发" not in loop.events
    assert loop.dumped == ["briefing-unrecognised"]


def test_the_two_gates_do_not_share_a_refusal_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两道闸门在 `refused` 里必须说得出各自的话，否则排障时分不开。"""
    mission = _loop(monkeypatch, origin_readings=[str(ORIGIN)], briefing="探索")
    mission.attack(TARGET, preset="AAA")
    drifted = _loop(monkeypatch, origin_readings=[str(MAIN_PLANET)])
    with pytest.raises(OriginDrifted):
        drifted.attack(TARGET, preset="AAA")

    assert mission._outcome.refused[0][1] != drifted._outcome.refused[0][1]


# -- (e) 读不出且屏上有弹窗 → 按弹窗类型走，不算起点漂了 ----------------------


def test_a_protection_dialog_skips_the_target_instead_of_killing_the_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**本节的重点**（生产 2026-08-27）：保护期弹窗盖住面板 ≠ 起点漂了。

    弹窗就压在起点那一行上，那一格自然读空。当成漂了的代价是整轮作废，而且
    **一笔保护期都没记下**——于是同一个目标下一轮又被挑中，撞同一个弹窗，
    一天十四次。

    这里断言的是「回到既有的那一档」：跳过这个目标、这一轮继续。
    """
    loop = _loop(monkeypatch, origin_readings=[""], dialog=pirate_ui.DIALOG_NO_MISSION)

    assert loop.attack(TARGET, preset="AAA") is False, "整轮被杀掉了"

    assert "click:出发" not in loop.events, "起点读不出还把舰队派了出去"
    assert "click:关闭弹窗" in loop.events, "弹窗没点掉，下一发照样被它挡着"
    assert loop.dumped == [], "这不是核不过，不该按核不过留现场"
    # 弹窗点掉之后画面停在派遣面板/列表上，和「弹窗挡下」那一支一样要自己退出来——
    # 不退的话下一个目标的 `goto` 会在列表页上朝导航栏盲点（实机点到过「取消」）。
    assert "离开飞行中列表" in loop.events


def test_the_protection_period_actually_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **这一笔是这个循环唯一的出口。**

    `bot_targets.protection_seen_at_utc` 是保护期唯一的证据——游戏那 8 小时推不
    出来，只能撞上了才知道。不写进去，选靶那边查不到，`4:268:5` 会被一轮一轮
    重新挑中（军力 10580、离得又近，排序上永远靠前）。

    生产实测：出事那几天这一列一直是 `NULL`，正是因为 `OriginDrifted` 抢在
    `_handle_dialog` 之前把这一轮结束掉了。
    """
    loop = _loop(monkeypatch, origin_readings=[""], dialog=pirate_ui.DIALOG_NO_MISSION)

    loop.attack(TARGET, preset="AAA")

    assert loop._repository.protections == [TARGET]


def test_a_protection_dialog_skips_the_scout_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """侦察那一支同样会撞上——两个调用点必须一起改，只改一个是半个修。"""
    loop = _loop(
        monkeypatch,
        origin_readings=[""],
        briefing="侦察",
        dialog=pirate_ui.DIALOG_NO_MISSION,
    )

    assert loop.scout(TARGET) is False
    assert "click:确认终点" not in loop.events
    assert loop._repository.protections == [TARGET]


def test_the_round_memory_survives_a_protection_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 「本轮已经切到哪」这份记忆**不该清**。

    清掉是「记忆被证伪」那一档的动作，而这一档恰恰相反：星球切对了，只是被弹窗
    盖住看不见。清掉的代价是下一个目标白切一次星球（约 3 秒 × 每颗），而这一轮
    还要接着打十几个目标。
    """
    loop = _loop(monkeypatch, origin_readings=[""], dialog=pirate_ui.DIALOG_NO_MISSION)

    loop.attack(TARGET, preset="AAA")

    assert loop._current_planet == ORIGIN


def test_an_exhausted_dialog_still_stops_the_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 另一半：「未选择任何战舰」是资源耗尽，**照旧停轮**。

    两类弹窗处理方式相反（`pirate_ui.DialogKind`）。把这一档也放成「跳过这个
    目标」，一轮会拿着一支派不出去的舰队把余下每个目标都空跑一遍。

    ⚠️ 停的形式是 `RoundExhausted` 而**不是** `OriginDrifted`：起点根本没漂，
    报成漂了会让排障从「星球切错了」那条死路开始查。
    """
    loop = _loop(monkeypatch, origin_readings=[""], dialog=pirate_ui.DIALOG_NO_SHIPS)

    with pytest.raises(RoundExhausted) as caught:
        loop.attack(TARGET, preset="AAA")

    assert not isinstance(caught.value, OriginDrifted)
    assert "click:出发" not in loop.events


def test_an_unreadable_origin_with_no_dialog_still_stops_the_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **2026-08-18 那道闸门必须原样还在。**

    这一次的放宽只多认了一档：「读不出**而且**屏上有认得的弹窗」。屏上什么都
    没有时读不出，仍旧说不出脚底下是哪一颗——照旧按核不过收场、照旧停这一轮。

    少了这条，上面那几条用例会在闸门被整个拆掉时全部照绿。
    """
    loop = _loop(monkeypatch, origin_readings=[""], dialog="")

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")

    assert loop.dumped == ["origin-mismatch"]
    assert loop._repository.protections == []


def test_a_drifted_origin_does_not_get_excused_by_a_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **读出来是别的坐标时不问弹窗。**

    弹窗解释得了「读空」，解释不了「读到 4:277:15」——那一行是从面板上读出来的
    真坐标，说明脚底下真的换了星球。此时若也交给 `_handle_dialog`，撞上保护期就
    会把一次真正的起点漂移降级成「跳过这个目标」，而这一轮余下每一发都会从错的
    星球飞出去、按对的星球记账（那正是 2026-08-18 事故的全貌）。
    """
    loop = _loop(
        monkeypatch,
        origin_readings=[str(MAIN_PLANET)],
        dialog=pirate_ui.DIALOG_NO_MISSION,
    )

    with pytest.raises(OriginDrifted):
        loop.attack(TARGET, preset="AAA")

    assert loop._repository.protections == [], "把真漂了的那一发记成了保护期"
    assert loop._current_planet is None
