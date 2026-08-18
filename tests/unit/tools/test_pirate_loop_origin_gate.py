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

## 本文件钉住的四条

1. 起点与期望一致 → 照常派（攻击、侦察各一条）。
2. 不一致 → 不派、记 `refused`、留现场、写 `system_log`、停下这一轮。
3. **读不出要重读；重读完仍读不出按「核不过」收场，绝不当成一致。**
4. 简报任务类型那道既有闸门不受影响——两道闸门各拦各的，谁也别顶替谁。
"""

from __future__ import annotations

from typing import Any

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
    loop._run_id = None
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
