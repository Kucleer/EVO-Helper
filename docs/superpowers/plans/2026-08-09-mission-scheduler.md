# 控制台任务调度器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把网页控制台从「建计划 + 定时窗口」改成「三条任务链路 + 拖拽优先级 + 开始/结束」的常驻调度器。

**Architecture:** 调度判据全部做成 `domain/` 里的纯函数（不碰 IO、不碰进程），事实一律从数据库读；`MissionSupervisor` 住在单进程的 web 服务里管子进程起停；三个 runner 保持独立进程、单趟即退。权威的航线闸门仍留在 runner 的 `LineCapacityGate` 里，调度器只做乐观估算。

**Tech Stack:** Python 3.12、SQLAlchemy 2.0 + Alembic、FastAPI + Jinja2、pytest、ruff、mypy。

**规格：** [docs/superpowers/specs/2026-08-09-mission-scheduler-design.md](../specs/2026-08-09-mission-scheduler-design.md)

**每个任务结束前的基线（不许退化）：**

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m mypy src
```

代码注释与文档一律中文，源文件带 `# -*- coding: utf-8 -*-` 不是本仓惯例（本仓用 `from __future__ import annotations`），读写文件一律 UTF-8。

---

## 并行波次

| 波次 | 任务 | 触碰的文件 | 依赖 |
|---|---|---|---|
| 1 | Task 1–3（单元 B） | `tools/pirate_loop.py`、`tools/bot_loop.py` | 无 |
| 1 | Task 4（单元 D） | `domain/missions.py`（新） | 无 |
| 1 | Task 5（单元 A） | `domain/scheduler.py`（新） | 无 |
| 1 | Task 6–7（单元 C） | `storage/models.py`、`storage/repository.py`、`alembic/` | 无 |
| 2 | Task 8–9（单元 E） | `application/mission_supervisor.py`（新）、`web/runtime.py` | 4、5、6、7 |
| 3 | Task 10（单元 F） | `web/schemas.py`、`web/persistent_service.py`、`web/app.py` | 8、9 |
| 3 | Task 11–12（单元 G） | `web/templates/missions.html`、`.changes/` | 10 |

波次 1 的四组任务文件互不重叠，可同时开工。

---

# 第一段：修正攻击记录的三个缺陷

## Task 1: `target_kind` 改为可被子类覆盖

**背景：** `BotLoop` 是 `PirateLoop` 的子类，写库走继承来的 `_record_intent`，那里 `target_kind=TARGET_KIND_PIRATE` 硬编码。结果是 bot 每打一发都占掉一格海盗的当日配额——而配额是游戏硬限制 32 次，数错了就白飞一趟舰队。

**Files:**
- Modify: `src/evo_helper/tools/pirate_loop.py`（类属性 + `_record_intent`）
- Modify: `src/evo_helper/tools/bot_loop.py`（覆盖类属性）
- Test: `tests/unit/tools/test_bot_loop.py`

- [ ] **Step 1: 写失败的测试**

追加到 `tests/unit/tools/test_bot_loop.py` 末尾：

```python
def test_bot_attacks_are_labelled_bot_not_pirate() -> None:
    """BotLoop 继承 PirateLoop 的写库路径，标签必须跟着子类走。

    标错的代价不是「日志难看」：海盗每天 32 次是游戏硬限制，bot 的发数
    混进去会让助手以为配额还没用完，多打的那一发会被强制返回。
    """
    from evo_helper.domain.records import TARGET_KIND_BOT, TARGET_KIND_PIRATE
    from evo_helper.tools.bot_loop import BotLoop
    from evo_helper.tools.pirate_loop import PirateLoop

    assert PirateLoop.TARGET_KIND == TARGET_KIND_PIRATE
    assert BotLoop.TARGET_KIND == TARGET_KIND_BOT
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m pytest tests/unit/tools/test_bot_loop.py::test_bot_attacks_are_labelled_bot_not_pirate -v`
Expected: FAIL — `AttributeError: type object 'PirateLoop' has no attribute 'TARGET_KIND'`

- [ ] **Step 3: 加类属性并让 `_record_intent` 用它**

在 `src/evo_helper/tools/pirate_loop.py` 的 `class PirateLoop` 定义体开头（紧跟类文档字符串之后）加上：

```python
    #: 这条链路打的是什么目标。子类覆盖它——`BotLoop` 走的是同一套写库路径，
    #: 标签却必须不同：海盗每天 32 次是游戏硬限制，两者混在一起会数错配额。
    TARGET_KIND: str = TARGET_KIND_PIRATE
```

把 `_record_intent` 里的

```python
                target_kind=TARGET_KIND_PIRATE,
```

改成

```python
                target_kind=self.TARGET_KIND,
```

在 `src/evo_helper/tools/bot_loop.py` 的 `class BotLoop(PirateLoop):` 文档字符串之后加上：

```python
    TARGET_KIND: str = TARGET_KIND_BOT
```

并在该文件的 import 区加入：

```python
from evo_helper.domain.records import TARGET_KIND_BOT
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/tools/test_bot_loop.py -v`
Expected: PASS（含原有 4 条）

- [ ] **Step 5: 跑全量基线**

Run: `python -m pytest tests -q && python -m ruff check src tests && python -m mypy src`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add src/evo_helper/tools/pirate_loop.py src/evo_helper/tools/bot_loop.py tests/unit/tools/test_bot_loop.py
git commit -m "修正 bot 攻击被错标成海盗

BotLoop 继承 PirateLoop 的 _record_intent，那里 target_kind 硬编码为
pirate。海盗每天 32 次是游戏硬限制，bot 的发数混进去会让助手以为配额
还没用完，多打的那一发会被游戏强制返回。改成类属性，子类各自覆盖。"
```

---

## Task 2: 把简报里的飞行时间写进 dispatch

**背景：** `attack_dispatches.expected_report_at_utc` 从来没被写入过（实测库中 4 条派遣全为 NULL）。简报数据在 `_launch()` 里已经读到（`DispatchBriefing.expected_report_at_utc`），只是没传出来。这一列是调度器决定「什么时候回来收战报」的唯一依据，不写它整个松手等待就是死的。

仓储侧的写入方法 `record_flight_time(dispatch_id, flight, dispatched_at_utc)` 已经存在（`storage/repository.py:416`），读不到飞行时间时写 NULL 是它既定的降级语义。

**Files:**
- Modify: `src/evo_helper/tools/pirate_loop.py`（`_launch` 返回简报、`_record_dispatch` 接收它、`attack()` 传递）
- Test: `tests/unit/tools/test_pirate_loop_dispatch_record.py`（新）

- [ ] **Step 1: 读懂现状**

Run: `grep -n "def _launch" -A 30 src/evo_helper/tools/pirate_loop.py`

确认 `_launch` 内部拿到了 `DispatchBriefing` 对象，且当前返回 `bool`。记下它内部那个简报变量的名字——下一步要把它返回出来。

- [ ] **Step 2: 写失败的测试**

新建 `tests/unit/tools/test_pirate_loop_dispatch_record.py`：

```python
"""派遣记录必须带上「战报什么时候到」。

这一列是助手松手之后唯一的回程闹钟（见 `domain.report_wait` 的模块头）。
不写它，等待调度器会把每一发都当成「立刻去收」，于是助手在战报还没产生时
反复登录——既白跑，又要和用户抢会话。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4


class _RecordingRepository:
    """只记下 `record_flight_time` 收到了什么。"""

    def __init__(self) -> None:
        self.saved_dispatch: object | None = None
        self.flight_calls: list[tuple[UUID, timedelta | None, datetime]] = []

    def save_dispatch(self, dispatch: object) -> None:
        self.saved_dispatch = dispatch

    def record_flight_time(
        self, dispatch_id: UUID, flight: timedelta | None, dispatched_at_utc: datetime
    ) -> None:
        self.flight_calls.append((dispatch_id, flight, dispatched_at_utc))


def _loop_with(repository: _RecordingRepository) -> object:
    from evo_helper.tools.pirate_loop import PirateLoop

    loop = PirateLoop.__new__(PirateLoop)  # 不跑 __init__：这里只测记录这一段
    loop._repository = repository  # type: ignore[attr-defined]
    loop._run_id = uuid4()  # type: ignore[attr-defined]
    return loop


def test_the_flight_time_from_the_briefing_reaches_the_database() -> None:
    from evo_helper.vision.parsers import DispatchBriefing, MissionType

    repository = _RecordingRepository()
    loop = _loop_with(repository)
    now = datetime.now(UTC)
    briefing = DispatchBriefing(
        mission_type=MissionType.ATTACK,
        flight=timedelta(minutes=7),
        arrival_at_utc=now + timedelta(minutes=7),
    )

    loop._record_dispatch(uuid4(), briefing)  # type: ignore[attr-defined]

    assert len(repository.flight_calls) == 1
    _dispatch_id, flight, _dispatched = repository.flight_calls[0]
    assert flight == timedelta(minutes=7)


def test_an_unreadable_briefing_still_records_the_dispatch_with_no_flight_time() -> None:
    """读不到简报不能吞掉派遣记录——那一发是真派出去了。

    飞行时间写 NULL，等待调度器据此改为「立即尝试收取」，
    而不是无限等一个不知道何时抵达的战报。
    """
    repository = _RecordingRepository()
    loop = _loop_with(repository)

    loop._record_dispatch(uuid4(), None)  # type: ignore[attr-defined]

    assert repository.saved_dispatch is not None
    assert len(repository.flight_calls) == 1
    _dispatch_id, flight, _dispatched = repository.flight_calls[0]
    assert flight is None
```

- [ ] **Step 3: 跑测试确认它失败**

Run: `python -m pytest tests/unit/tools/test_pirate_loop_dispatch_record.py -v`
Expected: FAIL — `TypeError: _record_dispatch() takes 2 positional arguments but 3 were given`

- [ ] **Step 4: 让 `_launch` 把简报返回出来**

在 `src/evo_helper/tools/pirate_loop.py` 里，把 `_launch` 的签名与返回值从 `bool` 改成 `DispatchBriefing | None`：

- 原先 `return True` 的成功分支，改为 `return briefing`（用 Step 1 记下的那个变量名）。
- 原先 `return False` 的各个失败分支，一律改为 `return None`。

在文件顶部的 import 区补上：

```python
from evo_helper.vision.parsers import DispatchBriefing
```

给 `_launch` 的文档字符串补一句，说明为什么返回值变了：

```python
        """点「出发！」并过简报闸门。

        返回简报而不是 `True`：简报上的抵达时间是助手松手之后唯一的回程闹钟，
        闸门读到了却不往外传，那一列就永远是 NULL（实测库里 4 条派遣全空）。
        闸门没过就返回 None。
        """
```

- [ ] **Step 5: 让 `_record_dispatch` 接收简报并写飞行时间**

把 `_record_dispatch` 整个替换为：

```python
    def _record_dispatch(self, intent_id: UUID, briefing: DispatchBriefing | None) -> None:
        """记下这一发，并把简报上的抵达时间存成回程闹钟。

        读不到简报时飞行时间写 NULL——`ReportWaitPlanner` 把「未知」当成
        「立即尝试收取」，而不是无限等一个不知道何时抵达的战报。
        """
        repository, _run_id = self._ensure_run()
        dispatch_id = uuid4()
        dispatched_at = datetime.now(UTC)
        repository.save_dispatch(
            AttackDispatch(
                dispatch_id=dispatch_id,
                intent_id=intent_id,
                dispatched_at_utc=dispatched_at,
                dry_run=False,
                accepted=True,
            )
        )
        repository.record_flight_time(
            dispatch_id,
            briefing.flight if briefing is not None else None,
            dispatched_at,
        )
```

- [ ] **Step 6: 改 `attack()` 的调用点**

在 `attack()` 里，把

```python
        if not self._launch(coordinate, "攻击"):
            self._leave_dispatch_list()
            return False
        self._record_dispatch(intent_id)
```

改成

```python
        briefing = self._launch(coordinate, "攻击")
        if briefing is None:
            self._leave_dispatch_list()
            return False
        self._record_dispatch(intent_id, briefing)
```

- [ ] **Step 7: 改其余所有 `_launch` 调用点**

Run: `grep -n "_launch(" src/evo_helper/tools/pirate_loop.py src/evo_helper/tools/bot_loop.py`

每一处 `if not self._launch(...)` 都改成先接住返回值再判 `is None`（侦察那条链路不写 dispatch，只需把布尔判断换成 `is None`）。

- [ ] **Step 8: 跑测试确认通过**

Run: `python -m pytest tests/unit/tools/ -v`
Expected: PASS

- [ ] **Step 9: 跑全量基线**

Run: `python -m pytest tests -q && python -m ruff check src tests && python -m mypy src`
Expected: 全绿

- [ ] **Step 10: 提交**

```bash
git add src/evo_helper/tools/pirate_loop.py tests/unit/tools/test_pirate_loop_dispatch_record.py
git commit -m "派遣记录带上简报里的抵达时间

expected_report_at_utc 从来没被写入过——实测库里 4 条派遣全是 NULL。
简报在 _launch() 里已经读到了，只是没传出来，于是助手松手之后唯一的
回程闹钟一直是空的。让 _launch 返回简报而不是 bool，_record_dispatch
接住它并调 record_flight_time。读不到简报时仍写 NULL，保持既定的
「立即尝试收取」降级语义。"
```

---

## Task 3: `bot_loop` 改为「派出即退出」

**背景：** `bot_loop` 每个目标 `time.sleep(600)` 等战报，期间独占鼠标——5 个目标就是 50 分钟，扫描一次也插不进去。这与「等待攻击路线时进行扫描」的需求直接冲突。改为一趟只推进每个目标一态，把回程时间交给数据库。

目标的三态从库里推导，**不新增列**：本轮该目标的 `attack_intents` 里 `preset_name == PROBE_PRESET` 的是探路发，等于分档预设（AAA / BBB / CCC）的是攻击发。

**Files:**
- Create: `src/evo_helper/domain/bot_round.py`
- Test: `tests/unit/domain/test_bot_round.py`
- Modify: `src/evo_helper/tools/bot_loop.py`

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/domain/test_bot_round.py`：

```python
"""bot 目标在一轮里走的三态。

态从库里推导而不是新增列：`preset_name` 已经把两种派遣分开了——
探路发用「探路」，攻击发用分档预设（AAA/BBB/CCC）。多一列就多一处
可能和事实对不上的地方。
"""

from __future__ import annotations

from evo_helper.domain.bot_round import BotPhase, DispatchFact, phase_of


def test_a_target_with_no_dispatch_this_round_needs_a_probe() -> None:
    assert phase_of(()) is BotPhase.NEEDS_PROBE


def test_a_probe_still_in_flight_means_wait_for_its_report() -> None:
    facts = (DispatchFact(preset_name="探路", has_report=False),)

    assert phase_of(facts) is BotPhase.AWAITING_PROBE_REPORT


def test_a_returned_probe_report_means_tier_and_attack() -> None:
    facts = (DispatchFact(preset_name="探路", has_report=True),)

    assert phase_of(facts) is BotPhase.NEEDS_ATTACK


def test_an_attack_in_flight_means_wait_for_its_report() -> None:
    facts = (
        DispatchFact(preset_name="探路", has_report=True),
        DispatchFact(preset_name="BBB", has_report=False),
    )

    assert phase_of(facts) is BotPhase.AWAITING_ATTACK_REPORT


def test_a_returned_attack_report_completes_the_target() -> None:
    facts = (
        DispatchFact(preset_name="探路", has_report=True),
        DispatchFact(preset_name="BBB", has_report=True),
    )

    assert phase_of(facts) is BotPhase.DONE


def test_a_target_judged_not_worth_attacking_is_done_not_stuck() -> None:
    """分档判定「2K 以下不派」的目标没有攻击发，但它已经走完流程。

    把它算成未完成，任务 2 就永远结束不了。
    """
    facts = (DispatchFact(preset_name="探路", has_report=True, skipped=True),)

    assert phase_of(facts) is BotPhase.DONE
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m pytest tests/unit/domain/test_bot_round.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'evo_helper.domain.bot_round'`

- [ ] **Step 3: 写实现**

新建 `src/evo_helper/domain/bot_round.py`：

```python
"""bot 目标在一轮里的推进状态。

纯函数：只看「这个目标本轮派过哪些发、各自的战报回来了没有」，
不碰数据库也不碰屏幕。调度器和 runner 用的是同一份判据，
两边对同一个目标的看法不会分叉。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

#: 攻击侦查用的预设标题。与 `tools.bot_loop.PROBE_PRESET` 同源，
#: 但这里不 import 那个模块——domain 层不依赖 tools 层。
PROBE_PRESET_NAME = "探路"


class BotPhase(Enum):
    """一个目标本轮走到哪一步了。"""

    #: 本轮还没碰过它。
    NEEDS_PROBE = "NEEDS_PROBE"
    #: 探路已派出，等它的战报。
    AWAITING_PROBE_REPORT = "AWAITING_PROBE_REPORT"
    #: 探路战报回来了，该分档并真打。
    NEEDS_ATTACK = "NEEDS_ATTACK"
    #: 攻击已派出，等它的战报。
    AWAITING_ATTACK_REPORT = "AWAITING_ATTACK_REPORT"
    #: 走完了。含「分档判定不值得打」而没派攻击的目标。
    DONE = "DONE"


@dataclass(frozen=True)
class DispatchFact:
    """本轮针对某个目标的一次派遣。"""

    preset_name: str
    has_report: bool
    #: 分档判定为「不值得打」，本轮不会再有攻击发。
    skipped: bool = False


def phase_of(dispatches: Sequence[DispatchFact]) -> BotPhase:
    """这个目标本轮该干什么。

    判据只看预设标题：探路发用「探路」，攻击发用分档预设。
    """
    if not dispatches:
        return BotPhase.NEEDS_PROBE

    probes = [item for item in dispatches if item.preset_name == PROBE_PRESET_NAME]
    attacks = [item for item in dispatches if item.preset_name != PROBE_PRESET_NAME]

    if attacks:
        return BotPhase.DONE if all(item.has_report for item in attacks) else (
            BotPhase.AWAITING_ATTACK_REPORT
        )

    if any(item.skipped for item in probes):
        # 分档说不值得打。它不会再产生攻击发，算走完。
        return BotPhase.DONE

    if not probes:
        return BotPhase.NEEDS_PROBE

    return BotPhase.NEEDS_ATTACK if all(item.has_report for item in probes) else (
        BotPhase.AWAITING_PROBE_REPORT
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/domain/test_bot_round.py -v`
Expected: PASS（6 条）

- [ ] **Step 5: 提交纯函数这一层**

```bash
git add src/evo_helper/domain/bot_round.py tests/unit/domain/test_bot_round.py
git commit -m "bot 目标本轮状态的纯函数判据

三态从 preset_name 推导（探路 vs 分档预设），不新增列。
分档判定「不值得打」的目标算走完——把它当未完成，任务 2 就永远结束不了。"
```

- [ ] **Step 6: 拆掉 `bot_loop` 的进程内干睡**

在 `src/evo_helper/tools/bot_loop.py` 里：

删除常量 `REPORT_WAIT_S` 及其注释，改写为：

```python
#: 攻击侦查用的预设标题：探路（`domain.fleet_preset.DEFAULT_PRESET`）。
PROBE_PRESET = DEFAULT_PRESET.name
```

把 `run()` 的循环体改成「一趟只推进一态」。用 `phase_of` 决定这个目标这一趟做什么：

```python
    def run(self) -> Any:  # noqa: D401 - 覆盖父类的海盗循环
        """一趟只把每个目标推进一态，然后退出。

        **不在进程内等战报。** 原先每个目标 `time.sleep(600)`，五个目标就是
        五十分钟独占鼠标，而这段时间本该拿去跑扫描。抵达时间已经写进
        `attack_dispatches.expected_report_at_utc`，到点由调度器把这条链路
        重新叫起来——这正是 `domain.report_wait` 模块头写的那条路。
        """
        from evo_helper.domain.bot_round import BotPhase, phase_of
        from evo_helper.game.game_window import ensure_game_window

        ensure_game_window()
        self._reset_to_known_screen()
        if not self._navigator.ensure_system_view(self._nav_labels):
            raise RuntimeError("切不到恒星系视图；停止而不是往固定坐标乱点")

        for coordinate in self._bot.targets:
            phase = phase_of(self._dispatch_facts(coordinate))
            say(f"目标 {coordinate}（{phase.value}）")
            if phase is BotPhase.NEEDS_PROBE:
                self._probe(coordinate)
            elif phase is BotPhase.NEEDS_ATTACK:
                self._tier_and_attack(coordinate)
            # 其余三态这一趟没事可做：等战报，或已走完。
        return self._outcome
```

- [ ] **Step 7: 加三个辅助方法**

在 `BotLoop` 里加上（放在 `run()` 之后）：

```python
    def _dispatch_facts(self, coordinate: Coordinate) -> tuple[Any, ...]:
        """本轮针对这个目标已经派过哪些发、战报回来了没有。"""
        repository, _run_id = self._ensure_run()
        return tuple(repository.bot_dispatch_facts(coordinate, since=self._bot.round_started_at))

    def _probe(self, coordinate: Coordinate) -> None:
        """派一发探路。走的是攻击链路，所以简报上写的是「攻击」。"""
        self._navigator.goto(coordinate)
        if not self.is_bot_target(coordinate):
            return
        self._outcome.pirates.append(coordinate)
        if not self._bot.probe:
            return
        if self.attack(coordinate, preset=PROBE_PRESET):
            self._outcome.scouted.append(coordinate)

    def _tier_and_attack(self, coordinate: Coordinate) -> None:
        """探路战报已回：读守方单位数、分档、按档位真打。"""
        if not self._bot.attack:
            return
        units = self.read_defender_units(coordinate)
        if units is None:
            say(f"  {coordinate} 读不到战报里的守方单位数；不打")
            self._outcome.refused.append((coordinate, "读不到守方单位数"))
            return
        tier = tier_for(units)
        preset = tier.preset
        if preset is None:
            say(f"  {coordinate} 守方 {units} 单位，{tier.name}；不值得打")
            self._outcome.refused.append((coordinate, f"{tier.name}：不值得打"))
            self._mark_skipped(coordinate)
            return
        self._navigator.goto(coordinate)
        if not self.is_bot_target(coordinate):
            self._outcome.refused.append((coordinate, "攻击前面板认不出"))
            return
        self.attack(coordinate, preset=preset)

    def _mark_skipped(self, coordinate: Coordinate) -> None:
        """把「分档说不值得打」记进库，否则下一趟又会重新分一次档。"""
        repository, _run_id = self._ensure_run()
        repository.mark_bot_target_skipped(coordinate, since=self._bot.round_started_at)
```

- [ ] **Step 8: 给 `BotOptions` 加 `round_started_at`**

`BotOptions` 需要知道「本轮从什么时候算起」，否则上一轮的战报会被当成本轮的。在 `bot_loop.py` 的 `BotOptions` 定义里加：

```python
    #: 本轮从何时算起。早于这个时刻的派遣属于上一轮，不参与本轮判态。
    round_started_at: datetime | None = None
```

并在 `main()` 里加命令行参数：

```python
    parser.add_argument(
        "--round-started-at",
        type=datetime.fromisoformat,
        default=None,
        help="本轮起始时刻（ISO 8601，UTC）。调度器会传；手工跑可以不给",
    )
```

以及构造 options 时：

```python
    options = BotOptions(
        targets=tuple(args.targets),
        probe=args.probe,
        attack=args.attack,
        round_started_at=args.round_started_at,
    )
```

补上 import：`from datetime import datetime`。

- [ ] **Step 9: 跑基线**

`bot_dispatch_facts` 与 `mark_bot_target_skipped` 由 Task 7 提供。若 Task 7 尚未合入，本步骤的全量测试会在集成层报 `AttributeError`——这是预期的，两个任务在波次 1 并行，合流后再一起验。单元测试必须已经通过：

Run: `python -m pytest tests/unit -q && python -m ruff check src tests`
Expected: 全绿

- [ ] **Step 10: 提交**

```bash
git add src/evo_helper/tools/bot_loop.py
git commit -m "bot 改为派出即退出，不在进程内等战报

原先每个目标 time.sleep(600)，五个目标就是五十分钟独占鼠标——而用户
明确要求「等待攻击路线时进行扫描」。抵达时间已经写进
expected_report_at_utc，到点由调度器把这条链路重新叫起来。
一趟只把每个目标推进一态，态由 domain.bot_round.phase_of 判定。"
```

---

# 第二段：调度核心

## Task 4: `domain/missions.py` —— 参数换算（单元 D，可与 1–3 并行）

**Files:**
- Create: `src/evo_helper/domain/missions.py`
- Test: `tests/unit/domain/test_missions.py`

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/domain/test_missions.py`：

```python
"""任务参数到命令行的换算。

纯函数：不碰数据库、不碰进程。三条链路的参数形状彼此不通
（扫描不吃参数、bot 要完整坐标、海盗要恒星系），换算集中在这里。
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


def test_pirate_systems_are_ordered_nearest_first() -> None:
    """由近到远；等距时小的在前——排序必须确定，否则测不住。"""
    systems = pirate_systems(ORIGIN, radius=2)

    assert systems == ((2, 137), (2, 136), (2, 138), (2, 135), (2, 139))


def test_a_radius_past_the_edge_is_clamped_not_rejected() -> None:
    """半径填大了应当是「到边为止」，不是「不许开始」。"""
    systems = pirate_systems(Coordinate(2, 2, 1), radius=5)

    assert min(system for _galaxy, system in systems) == 1


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


def test_commands_use_the_module_entry_points() -> None:
    assert scan_command()[-2:] == ["-m", "evo_helper.tools.scan_coordinates"]
    assert "--systems" in pirate_command(((2, 137),))
    assert "2:137" in pirate_command(((2, 137),))
    assert "2:137:14" in bot_command((Coordinate(2, 137, 14),))


def test_an_over_long_command_line_is_rejected_rather_than_truncated() -> None:
    """Windows CreateProcess 有 32767 字符上限。

    截断成「只打了前一半」比报错危险得多——它看起来成功了。
    """
    many = tuple(Coordinate(2, system, 1) for system in range(1, 4000))

    with pytest.raises(MissionParamError):
        bot_command(many)
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m pytest tests/unit/domain/test_missions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'evo_helper.domain.missions'`

- [ ] **Step 3: 写实现**

新建 `src/evo_helper/domain/missions.py`：

```python
"""任务参数到命令行的换算。

三条链路的参数形状彼此不通：扫描不吃参数（它自己管计划和游标），
bot 要完整坐标，海盗要恒星系。换算集中在这里，纯函数，
不碰数据库也不碰进程——调度器起进程之前先在这里把参数validate完。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from evo_helper.domain.models import Coordinate
from evo_helper.domain.scan_priority import SYSTEMS_PER_GALAXY

#: 主星。原先在 `tools.pirate_loop` 与 `tools.scan_coordinates` 各写了一遍。
ORIGIN = Coordinate(2, 137, 18)

#: 命令行长度上限。Windows `CreateProcess` 的硬上限是 32767 字符，留出余量。
#: 超了要报错而不是截断——截断成「只打了前一半」看起来是成功的。
MAX_COMMAND_CHARS = 30000


class MissionParamError(ValueError):
    """任务参数不合格。调度器据此拒绝启动，而不是拉起一个注定空转的进程。"""


def pirate_systems(origin: Coordinate, radius: int) -> tuple[tuple[int, int], ...]:
    """从主星向外排的恒星系清单，由近到远。

    等距时小的在前：排序必须是确定的，否则「上一轮打到哪了」无从谈起。
    越界的系号钳制到 `[1, SYSTEMS_PER_GALAXY]`——半径填大了应当是「到边为止」。
    """
    if radius < 1:
        raise MissionParamError(f"半径要大于 0（收到 {radius}）")
    low = max(1, origin.system - radius)
    high = min(SYSTEMS_PER_GALAXY, origin.system + radius)
    ordered = sorted(range(low, high + 1), key=lambda system: (abs(system - origin.system), system))
    return tuple((origin.galaxy, system) for system in ordered)


def bot_targets_in_range(
    targets: Sequence[Coordinate], *, galaxy: int, first_system: int, last_system: int
) -> tuple[Coordinate, ...]:
    """已记录的 bot 里落在这个恒星系区间内的那些。位次全要。"""
    if first_system > last_system:
        raise MissionParamError(f"恒星系区间首尾颠倒：{first_system} > {last_system}")
    return tuple(
        target
        for target in targets
        if target.galaxy == galaxy and first_system <= target.system <= last_system
    )


def scan_command() -> list[str]:
    """扫描不吃参数：它自己维护计划与游标（`tools.scan_coordinates`）。"""
    return _checked([sys.executable, "-u", "-m", "evo_helper.tools.scan_coordinates"])


def pirate_command(systems: Sequence[tuple[int, int]]) -> list[str]:
    if not systems:
        raise MissionParamError("没有可打的恒星系")
    listed = [f"{galaxy}:{system}" for galaxy, system in systems]
    return _checked(
        [sys.executable, "-u", "-m", "evo_helper.tools.pirate_loop", "--systems", *listed]
        + ["--scout", "--attack"]
    )


def bot_command(targets: Sequence[Coordinate]) -> list[str]:
    if not targets:
        raise MissionParamError("该范围内没有已记录的 bot；先跑扫描")
    listed = [f"{item.galaxy}:{item.system}:{item.position}" for item in targets]
    return _checked(
        [sys.executable, "-u", "-m", "evo_helper.tools.bot_loop", "--targets", *listed]
        + ["--probe", "--attack"]
    )


def _checked(command: list[str]) -> list[str]:
    length = sum(len(part) + 1 for part in command)
    if length > MAX_COMMAND_CHARS:
        raise MissionParamError(
            f"命令行 {length} 字符，超过 {MAX_COMMAND_CHARS} 上限；缩小范围再试"
        )
    return command
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/domain/test_missions.py -v`
Expected: PASS（8 条）

- [ ] **Step 5: 跑基线并提交**

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m mypy src
git add src/evo_helper/domain/missions.py tests/unit/domain/test_missions.py
git commit -m "任务参数到命令行的换算

三条链路参数形状不通，换算集中成纯函数。半径越界钳制而非报错；
空目标集、颠倒区间、超长命令行一律拒绝——命令行截断成「只打了前一半」
看起来是成功的，比报错危险得多。ORIGIN 从两处硬编码收成一份。"
```

---

## Task 5: `domain/scheduler.py` —— 调度判据（单元 A，可与 1–4 并行）

**Files:**
- Create: `src/evo_helper/domain/scheduler.py`
- Test: `tests/unit/domain/test_scheduler.py`

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/domain/test_scheduler.py`：

```python
"""调度判据：给定事实，下一步该起谁。

纯函数，不碰数据库、不碰进程、不看屏。用户描述的四个场景在这里逐条钉死。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.scheduler import (
    Action,
    MissionKind,
    SchedulerFacts,
    TaskSnapshot,
    decide,
    has_work,
)

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
DWELL = timedelta(seconds=60)


def facts(**overrides: object) -> SchedulerFacts:
    base = {
        "now": NOW,
        "free_lines": 1,
        "pirate_dispatches_today": 0,
        "pirate_quota": 32,
        "pirate_blocked_until": None,
        "pirate_reports_due": False,
        "bot_reports_due": False,
        "bot_targets_remaining": 5,
    }
    base.update(overrides)
    return SchedulerFacts(**base)  # type: ignore[arg-type]


def tasks(*kinds: MissionKind) -> tuple[TaskSnapshot, ...]:
    return tuple(
        TaskSnapshot(kind=kind, enabled=True, priority=index) for index, kind in enumerate(kinds)
    )


# -- has_work ------------------------------------------------------------------


def test_scanning_always_has_work() -> None:
    """扫描不派遣，因此永远有活干——它正是用来填空隙的。"""
    assert has_work(MissionKind.SCAN, facts(free_lines=0))


def test_pirates_stop_when_the_daily_quota_is_used_up() -> None:
    """每天 32 次是游戏硬限制，超了会被强制返回。"""
    assert not has_work(MissionKind.PIRATE, facts(pirate_dispatches_today=32))


def test_pirates_stop_when_the_game_said_the_quota_is_gone() -> None:
    """收到超限邮件时 runner 会写下封锁截止时刻，那是比计数更硬的信号。"""
    blocked = facts(pirate_blocked_until=NOW + timedelta(hours=3))

    assert not has_work(MissionKind.PIRATE, blocked)


def test_a_full_line_pool_does_not_stop_a_task_that_owes_a_report() -> None:
    """航线满了也要能回去收战报——收报告不占航线。"""
    assert has_work(MissionKind.PIRATE, facts(free_lines=0, pirate_reports_due=True))


def test_a_full_line_pool_stops_a_task_with_nothing_due() -> None:
    """这就是「前序占满航线时不开下一个」。"""
    assert not has_work(MissionKind.PIRATE, facts(free_lines=0))


def test_bots_are_done_when_no_target_remains() -> None:
    assert not has_work(MissionKind.BOT, facts(bot_targets_remaining=0))


# -- decide --------------------------------------------------------------------


def test_the_highest_priority_task_with_work_starts() -> None:
    """勾了 1-2-3：海盗优先。"""
    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.BOT, MissionKind.SCAN),
        facts(),
        running=None,
        min_dwell=DWELL,
    )

    assert decision == (Action.START, MissionKind.PIRATE)


def test_scanning_fills_the_gap_when_the_attack_tasks_are_blocked() -> None:
    """勾了 1-3：海盗配额用尽后，扫描顶上。"""
    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.SCAN),
        facts(pirate_dispatches_today=32),
        running=None,
        min_dwell=DWELL,
    )

    assert decision == (Action.START, MissionKind.SCAN)


def test_a_disabled_task_never_starts() -> None:
    snapshot = (
        TaskSnapshot(kind=MissionKind.PIRATE, enabled=False, priority=0),
        TaskSnapshot(kind=MissionKind.SCAN, enabled=True, priority=1),
    )

    decision = decide(snapshot, facts(), running=None, min_dwell=DWELL)

    assert decision == (Action.START, MissionKind.SCAN)


def test_an_auto_disabled_task_never_starts() -> None:
    """连续失败被自动停用的任务不该把调度循环拖成满速空转。"""
    snapshot = (
        TaskSnapshot(
            kind=MissionKind.PIRATE, enabled=True, priority=0, disabled_reason="连续 3 次异常退出"
        ),
        TaskSnapshot(kind=MissionKind.SCAN, enabled=True, priority=1),
    )

    decision = decide(snapshot, facts(), running=None, min_dwell=DWELL)

    assert decision == (Action.START, MissionKind.SCAN)


def test_scanning_is_preempted_once_an_attack_task_has_work() -> None:
    from evo_helper.domain.scheduler import RunningProcess

    running = RunningProcess(kind=MissionKind.SCAN, started_at=NOW - timedelta(seconds=90))

    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.SCAN), facts(), running=running, min_dwell=DWELL
    )

    assert decision == (Action.PREEMPT, MissionKind.PIRATE)


def test_scanning_is_not_preempted_before_the_minimum_dwell() -> None:
    """航线一空一占会引起秒级反复切换，而每次切换都要校几何 + 认屏。"""
    from evo_helper.domain.scheduler import RunningProcess

    running = RunningProcess(kind=MissionKind.SCAN, started_at=NOW - timedelta(seconds=10))

    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.SCAN), facts(), running=running, min_dwell=DWELL
    )

    assert decision == (Action.IDLE, None)


def test_an_attack_round_is_never_preempted() -> None:
    """中途杀掉可能正停在派遣面板上。攻击轮一旦启动就跑完。"""
    from evo_helper.domain.scheduler import RunningProcess

    running = RunningProcess(kind=MissionKind.BOT, started_at=NOW - timedelta(minutes=30))

    decision = decide(
        tasks(MissionKind.PIRATE, MissionKind.BOT), facts(), running=running, min_dwell=DWELL
    )

    assert decision == (Action.IDLE, None)


def test_nothing_to_do_is_idle_not_an_error() -> None:
    decision = decide(
        tasks(MissionKind.PIRATE),
        facts(pirate_dispatches_today=32),
        running=None,
        min_dwell=DWELL,
    )

    assert decision == (Action.IDLE, None)
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m pytest tests/unit/domain/test_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'evo_helper.domain.scheduler'`

- [ ] **Step 3: 写实现**

新建 `src/evo_helper/domain/scheduler.py`：

```python
"""调度判据：给定事实，下一步该起谁。

纯函数，不碰数据库、不碰进程、**不看屏**。所有事实由调用方从数据库读好传进来，
这样调度器看到的和 `/logs` 页面看到的是同一份东西。

一条硬不变量贯穿全篇：**任何时刻最多一个子进程在点鼠标**。一个游戏窗口，
一个鼠标。所以这里给出的永远是「下一步做一件事」，不是任务队列。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class MissionKind(Enum):
    PIRATE = "PIRATE"
    BOT = "BOT"
    SCAN = "SCAN"


class Action(Enum):
    #: 空闲，起一个新的。
    START = "START"
    #: 打断正在跑的扫描，换成 `kind`。
    PREEMPT = "PREEMPT"
    #: 什么都不做。
    IDLE = "IDLE"


@dataclass(frozen=True)
class TaskSnapshot:
    kind: MissionKind
    enabled: bool
    priority: int
    #: 连续失败被自动停用的原因。非空即视为不参与调度。
    disabled_reason: str | None = None


@dataclass(frozen=True)
class RunningProcess:
    kind: MissionKind
    started_at: datetime


@dataclass(frozen=True)
class SchedulerFacts:
    """一次调度所需的全部事实，全部来自数据库。

    `free_lines` 是**乐观估算**，不含用户自己派出去的舰队。权威的航线闸门
    在 runner 的 `game.capacity.LineCapacityGate` 里——它看屏。这里估高了，
    最坏结果是 runner 起来发现没位子、空跑一轮就退，不会误派。
    """

    now: datetime
    free_lines: int
    pirate_dispatches_today: int
    pirate_quota: int
    #: 收到游戏的超限邮件时写下的封锁截止时刻。比计数更硬的信号。
    pirate_blocked_until: datetime | None
    pirate_reports_due: bool
    bot_reports_due: bool
    bot_targets_remaining: int


@dataclass(frozen=True)
class Decision:
    action: Action
    kind: MissionKind | None = None

    def __iter__(self):  # type: ignore[no-untyped-def]
        """允许 `assert decide(...) == (Action.START, kind)` 这样写测试。"""
        yield self.action
        yield self.kind

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple):
            return (self.action, self.kind) == other
        if isinstance(other, Decision):
            return (self.action, self.kind) == (other.action, other.kind)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.action, self.kind))


def has_work(kind: MissionKind, facts: SchedulerFacts) -> bool:
    """这条链路现在有没有事可做。"""
    if kind is MissionKind.SCAN:
        # 扫描不派遣，因此不受航线约束，也没有完成态。它正是用来填空隙的。
        return True

    if kind is MissionKind.PIRATE:
        if facts.pirate_blocked_until is not None and facts.pirate_blocked_until > facts.now:
            return False
        if facts.pirate_dispatches_today >= facts.pirate_quota:
            return False
        return facts.free_lines > 0 or facts.pirate_reports_due

    if facts.bot_targets_remaining <= 0:
        return False
    return facts.free_lines > 0 or facts.bot_reports_due


def decide(
    tasks: Sequence[TaskSnapshot],
    facts: SchedulerFacts,
    *,
    running: RunningProcess | None,
    min_dwell: timedelta,
) -> Decision:
    """下一步该做什么。"""
    candidates = sorted(
        (task for task in tasks if task.enabled and task.disabled_reason is None),
        key=lambda task: task.priority,
    )
    wanted = next((task.kind for task in candidates if has_work(task.kind, facts)), None)

    if running is not None:
        # 抢占只有一条规则：只有扫描会被打断。攻击轮中途杀掉可能正停在派遣面板上。
        if (
            running.kind is MissionKind.SCAN
            and wanted is not None
            and wanted is not MissionKind.SCAN
            and facts.now - running.started_at >= min_dwell
        ):
            return Decision(Action.PREEMPT, wanted)
        return Decision(Action.IDLE)

    if wanted is None:
        return Decision(Action.IDLE)
    return Decision(Action.START, wanted)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/domain/test_scheduler.py -v`
Expected: PASS（14 条）

- [ ] **Step 5: 跑基线并提交**

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m mypy src
git add src/evo_helper/domain/scheduler.py tests/unit/domain/test_scheduler.py
git commit -m "调度判据：给定事实，下一步该起谁

纯函数，不碰库不碰进程不看屏。用户描述的四个场景逐条钉死：勾 123 的
流转、勾 13 的间歇填充、航线占满时的让位、配额用尽后只剩扫描。
free_lines 是乐观估算，权威闸门仍在 runner 的 LineCapacityGate。"
```

---

## Task 6: 三张新表与迁移（单元 C，可与 1–5 并行）

**Files:**
- Modify: `src/evo_helper/storage/models.py`
- Create: `alembic/versions/<hash>_mission_scheduler_tables.py`
- Test: `tests/integration/storage/test_mission_tables.py`

- [ ] **Step 1: 写失败的测试**

新建 `tests/integration/storage/test_mission_tables.py`：

```python
"""调度器的三张表。

`mission_tasks` 三行固定（每种任务一行），`mission_runs` 一次子进程一行，
`scheduler_config` 单行。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from evo_helper.storage import models as orm


def test_a_mission_task_row_round_trips(session_factory) -> None:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        session.add(
            orm.MissionTaskRow(
                kind="PIRATE",
                enabled=True,
                priority=0,
                params_json='{"radius": 10}',
                created_at_utc=datetime.now(UTC),
                updated_at_utc=datetime.now(UTC),
            )
        )
        session.commit()

    with session_factory() as session:
        row = session.scalar(select(orm.MissionTaskRow).where(orm.MissionTaskRow.kind == "PIRATE"))
        assert row is not None
        assert row.params_json == '{"radius": 10}'
        assert row.consecutive_failures == 0
        assert row.disabled_reason is None
        assert row.quota_exhausted_until_utc is None


def test_a_mission_run_row_records_how_it_ended(session_factory) -> None:  # type: ignore[no-untyped-def]
    started = datetime.now(UTC)
    with session_factory() as session:
        session.add(
            orm.MissionRunRow(
                kind="SCAN",
                command="python -m evo_helper.tools.scan_coordinates",
                pid=4242,
                started_at_utc=started,
                log_path="var/logs/mission-scan.log",
            )
        )
        session.commit()

    with session_factory() as session:
        row = session.scalar(select(orm.MissionRunRow))
        assert row is not None
        # 还在跑：结束相关的列全空，页面据此判断「运行中」。
        assert row.ended_at_utc is None
        assert row.exit_code is None
        assert row.stopped_by is None


def test_scheduler_config_carries_the_tunables(session_factory) -> None:  # type: ignore[no-untyped-def]
    with session_factory() as session:
        session.add(orm.SchedulerConfigRow(id=1))
        session.commit()

    with session_factory() as session:
        row = session.get(orm.SchedulerConfigRow, 1)
        assert row is not None
        assert row.pirate_daily_quota == 32
        assert row.min_dwell_seconds == 60
        assert row.report_grace_minutes == 30
```

- [ ] **Step 2: 确认 `session_factory` fixture 存在**

Run: `grep -rn "def session_factory" tests/`

若集成测试目录已有该 fixture，直接用；若没有，照同目录其他集成测试的建表方式补一个到 `tests/integration/storage/conftest.py`。

- [ ] **Step 3: 跑测试确认它失败**

Run: `python -m pytest tests/integration/storage/test_mission_tables.py -v`
Expected: FAIL — `AttributeError: module 'evo_helper.storage.models' has no attribute 'MissionTaskRow'`

- [ ] **Step 4: 加三个 ORM 类**

在 `src/evo_helper/storage/models.py` 末尾追加：

```python
class MissionTaskRow(Base):
    """三条任务链路各一行。优先级由用户在页面上拖出来。"""

    __tablename__ = "mission_tasks"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    #: `PIRATE` / `BOT` / `SCAN`
    kind: Mapped[str] = mapped_column(String(16), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    #: 升序即优先级。
    priority: Mapped[int] = mapped_column(Integer, default=0)
    #: 各链路自己的参数。海盗 `{"radius": N}`，bot
    #: `{"galaxy": G, "first_system": A, "last_system": B}`，扫描 `{}`。
    #: 存 JSON 而不是逐列：以后加任务种类不用再动表结构。
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    #: 仅 bot 用：本轮从何时算起。早于这个时刻的战报属于上一轮。
    round_started_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 仅海盗用：收到游戏超限邮件时写下的封锁截止时刻。比计数更硬的信号。
    quota_exhausted_until_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 连续异常退出次数。到阈值就自动停用，免得调度循环在一个坏掉的任务上空转。
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    disabled_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class MissionRunRow(Base):
    """调度器每起一个子进程记一行。"""

    __tablename__ = "mission_runs"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    #: 实际拉起的命令行。事后翻账时「那一轮到底打了谁」全靠它。
    command: Mapped[str] = mapped_column(Text)
    #: 用来在控制台重启后认出可能还活着的孤儿进程。
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: `USER` / `SELF` / `PREEMPTED` / `SHUTDOWN` / `UNKNOWN`
    stopped_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    log_path: Mapped[str] = mapped_column(String(255))


class SchedulerConfigRow(Base):
    """单行配置。航线是全局资源，不属于任何单个任务。"""

    __tablename__ = "scheduler_config"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    fleet_line_limit: Mapped[int] = mapped_column(Integer, default=1)
    reserved_lines: Mapped[int] = mapped_column(Integer, default=0)
    #: 游戏硬限制。超了会收到邮件且攻击被强制返回。
    pirate_daily_quota: Mapped[int] = mapped_column(Integer, default=32)
    #: 扫描起来后至少跑这么久才允许被抢占。防止航线一空一占引起秒级反复切换。
    min_dwell_seconds: Mapped[int] = mapped_column(Integer, default=60)
    #: 过了预计战报时间再等这么久仍读不到，就判为「战报缺失」跳过。
    report_grace_minutes: Mapped[int] = mapped_column(Integer, default=30)
```

若 `Text` 尚未在该文件的 import 里，补上：`from sqlalchemy import Text`。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/integration/storage/test_mission_tables.py -v`
Expected: PASS（3 条）

- [ ] **Step 6: 生成并检查迁移**

```bash
python -m alembic revision --autogenerate -m "mission scheduler tables"
```

打开生成的 `alembic/versions/<hash>_mission_scheduler_tables.py`，确认：

- `upgrade()` 里**只有**三个 `op.create_table`，没有对 `scan_plans` / `run_instances` / `attack_*` 的任何改动。**若有多余的改动，删掉它们**——autogenerate 有时会把既存的细微差异一并写进来，而本次改动明确不碰那些表。
- `downgrade()` 里对应三个 `op.drop_table`。

- [ ] **Step 7: 应用迁移并核对**

```bash
python -m alembic upgrade head
python -c "import sqlite3; c=sqlite3.connect('var/evo-helper.db'); print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'mission%' OR name='scheduler_config'\")])"
```

Expected: `['mission_tasks', 'mission_runs', 'scheduler_config']`

- [ ] **Step 8: 跑基线并提交**

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m mypy src
git add src/evo_helper/storage/models.py alembic/versions/ tests/integration/storage/test_mission_tables.py
git commit -m "调度器的三张表

mission_tasks 三行固定，params 存 JSON——以后加任务种类不用再动表结构。
mission_runs 记 pid，用来在控制台重启后认出可能还活着的孤儿进程。
scheduler_config 单行：航线是全局资源，不属于任何单个任务。
scan_plans / run_instances / attack_* 一律不动。"
```

---

## Task 7: 调度器要读的四个查询（单元 C 续）

**Files:**
- Modify: `src/evo_helper/storage/repository.py`
- Test: `tests/integration/storage/test_scheduler_queries.py`

- [ ] **Step 1: 写失败的测试**

新建 `tests/integration/storage/test_scheduler_queries.py`：

```python
"""调度器要问数据库的四件事。

这些查询是调度判据的事实来源。它们和 `/logs` 页面读的是同一批表——
判据和页面分叉，是这套东西最容易悄悄出错的地方。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
)


def test_todays_pirate_dispatches_are_counted_from_utc_midnight(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """重置点是 UTC 00:00，也就是本地 UTC+8 的每天早上 8 点。"""
    now = datetime.now(UTC)
    yesterday = now - timedelta(days=1)
    for dispatched_at in (yesterday, now):
        intent_id = uuid4()
        repository.save_attack_intent(
            AttackIntent(
                intent_id=intent_id,
                run_id=run_id,
                origin=Coordinate(2, 137, 18),
                target=Coordinate(2, 137, 1),
                preset=FleetPresetRef(name="AAA", signature="sig"),
                cycle_start_utc=dispatched_at,
                created_at_utc=dispatched_at,
                target_kind=TARGET_KIND_PIRATE,
            )
        )
        repository.save_dispatch(
            AttackDispatch(
                dispatch_id=uuid4(),
                intent_id=intent_id,
                dispatched_at_utc=dispatched_at,
                dry_run=False,
                accepted=True,
            )
        )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=_utc_midnight(now)) == 1


def test_bot_dispatches_do_not_count_towards_the_pirate_quota(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """标错就白飞一趟舰队——这条测试守的就是 Task 1 修的那个 bug。"""
    now = datetime.now(UTC)
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 140, 3),
            preset=FleetPresetRef(name="BBB", signature="sig"),
            cycle_start_utc=now,
            created_at_utc=now,
            target_kind=TARGET_KIND_BOT,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent_id,
            dispatched_at_utc=now,
            dry_run=False,
            accepted=True,
        )
    )

    assert repository.count_dispatches_since(TARGET_KIND_PIRATE, since=_utc_midnight(now)) == 0
    assert repository.count_dispatches_since(TARGET_KIND_BOT, since=_utc_midnight(now)) == 1


def test_pending_reports_are_scoped_by_target_kind(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """海盗和 bot 各等各的报告。混在一起，一条链路会替另一条判「该回去收了」。"""
    now = datetime.now(UTC)
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 137, 2),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=now,
            created_at_utc=now,
            target_kind=TARGET_KIND_PIRATE,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent_id,
            dispatched_at_utc=now,
            dry_run=False,
            accepted=True,
        )
    )

    assert len(repository.pending_reports_for_kind(TARGET_KIND_PIRATE)) == 1
    assert repository.pending_reports_for_kind(TARGET_KIND_BOT) == []


def test_a_dispatch_with_no_flight_time_is_reported_as_unknown(repository, run_id) -> None:  # type: ignore[no-untyped-def]
    """读不到飞行时间时 expected 为 None，等待调度器据此立即尝试收取。"""
    now = datetime.now(UTC)
    intent_id = uuid4()
    repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=Coordinate(2, 137, 18),
            target=Coordinate(2, 137, 3),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=now,
            created_at_utc=now,
            target_kind=TARGET_KIND_PIRATE,
        )
    )
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent_id,
            dispatched_at_utc=now,
            dry_run=False,
            accepted=True,
        )
    )

    pending = repository.pending_reports_for_kind(TARGET_KIND_PIRATE)

    assert pending[0].expected_report_at_utc is None


def _utc_midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)
```

- [ ] **Step 2: 确认 `repository` / `run_id` fixture 存在**

Run: `grep -rn "def repository\|def run_id" tests/integration/`

若不存在，照 `tests/integration/storage/` 下其他测试的建法补到该目录的 `conftest.py`。

- [ ] **Step 3: 跑测试确认它失败**

Run: `python -m pytest tests/integration/storage/test_scheduler_queries.py -v`
Expected: FAIL — `AttributeError: 'SqlAlchemyRepository' object has no attribute 'count_dispatches_since'`

- [ ] **Step 4: 加四个查询方法**

在 `src/evo_helper/storage/repository.py` 的 `SqlAlchemyRepository` 里，紧接 `pending_reports` 之后加：

```python
    # -- 调度器要问的事 --------------------------------------------------------

    def count_dispatches_since(self, target_kind: str, *, since: datetime) -> int:
        """某种目标在 `since` 之后真派出去了几发。

        海盗每天 32 次是游戏硬限制，超了会收到邮件且攻击被强制返回。
        只数**真实**派遣：演习记录不会消耗配额。
        """
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(orm.AttackDispatchRow)
                    .join(
                        orm.AttackIntentRow,
                        orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id,
                    )
                    .where(
                        orm.AttackIntentRow.target_kind == target_kind,
                        orm.AttackDispatchRow.accepted.is_(True),
                        orm.AttackDispatchRow.dry_run.is_(False),
                        orm.AttackDispatchRow.dispatched_at_utc >= _require_utc(since, "since"),
                    )
                )
                or 0
            )

    def pending_reports_for_kind(self, target_kind: str) -> list[PendingReport]:
        """某种目标下尚未闭合的派遣，供 `ReportWaitPlanner` 判「该等还是该收」。

        按 `target_kind` 分开：混在一起，一条链路会替另一条判「该回去收了」。
        """
        with self._session_factory() as session:
            rows = session.execute(
                select(orm.AttackDispatchRow, orm.BattleReportRow.id)
                .join(
                    orm.AttackIntentRow, orm.AttackIntentRow.id == orm.AttackDispatchRow.intent_id
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .where(
                    orm.AttackIntentRow.target_kind == target_kind,
                    orm.AttackDispatchRow.accepted.is_(True),
                    orm.AttackDispatchRow.dry_run.is_(False),
                )
                .order_by(orm.AttackDispatchRow.dispatched_at_utc)
            ).all()
            return [
                PendingReport(
                    dispatch_id=str(dispatch.id),
                    expected_report_at_utc=dispatch.expected_report_at_utc,
                    closed=report_id is not None,
                )
                for dispatch, report_id in rows
            ]

    def bot_dispatch_facts(self, coordinate: Coordinate, *, since: datetime | None) -> list[Any]:
        """本轮针对这个 bot 已经派过哪些发、战报回来了没有。

        供 `domain.bot_round.phase_of` 判态。`since` 为空表示不限本轮
        （手工跑命令行时用）。
        """
        from evo_helper.domain.bot_round import DispatchFact

        with self._session_factory() as session:
            statement = (
                select(
                    orm.AttackIntentRow.preset_name,
                    orm.BattleReportRow.id,
                    orm.AttackIntentRow.guard_status,
                )
                .join(
                    orm.AttackDispatchRow,
                    orm.AttackDispatchRow.intent_id == orm.AttackIntentRow.id,
                )
                .outerjoin(
                    orm.BattleReportRow,
                    orm.BattleReportRow.dispatch_id == orm.AttackDispatchRow.id,
                )
                .where(
                    orm.AttackIntentRow.target_kind == TARGET_KIND_BOT,
                    orm.AttackIntentRow.target_galaxy == coordinate.galaxy,
                    orm.AttackIntentRow.target_system == coordinate.system,
                    orm.AttackIntentRow.target_position == coordinate.position,
                )
            )
            if since is not None:
                statement = statement.where(
                    orm.AttackIntentRow.created_at_utc >= _require_utc(since, "since")
                )
            return [
                DispatchFact(
                    preset_name=preset_name,
                    has_report=report_id is not None,
                    skipped=guard_status == GUARD_STATUS_SKIPPED,
                )
                for preset_name, report_id, guard_status in session.execute(statement).all()
            ]

    def mark_bot_target_skipped(self, coordinate: Coordinate, *, since: datetime | None) -> None:
        """把「分档说不值得打」记在本轮那条探路意图上。

        不记的话，下一趟又会重新分一次档、重新读一次战报，而结论不会变。
        """
        with self._session_factory() as session:
            statement = select(orm.AttackIntentRow).where(
                orm.AttackIntentRow.target_kind == TARGET_KIND_BOT,
                orm.AttackIntentRow.target_galaxy == coordinate.galaxy,
                orm.AttackIntentRow.target_system == coordinate.system,
                orm.AttackIntentRow.target_position == coordinate.position,
            )
            if since is not None:
                statement = statement.where(
                    orm.AttackIntentRow.created_at_utc >= _require_utc(since, "since")
                )
            for row in session.scalars(statement.order_by(orm.AttackIntentRow.created_at_utc)):
                row.guard_status = GUARD_STATUS_SKIPPED
            session.commit()
```

在该文件顶部补上需要的名字：

```python
from sqlalchemy import func

from evo_helper.domain.records import TARGET_KIND_BOT

#: 分档判定「不值得打」。写在意图的 guard_status 上——那一列本来就是
#: 「这一发为什么没打出去」的记录位。
GUARD_STATUS_SKIPPED = "SKIPPED_NEGLIGIBLE"
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/integration/storage/test_scheduler_queries.py -v`
Expected: PASS（4 条）

- [ ] **Step 6: 跑基线并提交**

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m mypy src
git add src/evo_helper/storage/repository.py tests/integration/storage/test_scheduler_queries.py
git commit -m "调度器要问数据库的四件事

日配额按 target_kind 分开数——bot 混进海盗的计数会让助手以为配额还没
用完。待收战报也按 kind 分，否则一条链路会替另一条判「该回去收了」。
bot 的分档跳过记在 guard_status 上，免得下一趟重新分一次档。"
```

---

## Task 8: `MissionSupervisor`（单元 E，依赖 4–7）

**Files:**
- Create: `src/evo_helper/application/mission_supervisor.py`
- Test: `tests/unit/application/test_mission_supervisor.py`

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/application/test_mission_supervisor.py`：

```python
"""子进程的起停、抢占、自停。

照 `tests/unit/tools/test_scan_console.py` 的形状：假进程 + 可注入时钟，
起停逻辑是这里唯一有分支的部分，把它和 Win32、数据库都摘开才测得了。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.application.mission_supervisor import MissionSupervisor, StopReason
from evo_helper.domain.scheduler import MissionKind

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 4242
        self.terminated = False
        self._code: int | None = None

    def poll(self) -> int | None:
        return self._code

    def terminate(self) -> None:
        self.terminated = True
        self._code = -15

    def wait(self, timeout: float | None = None) -> int:
        return self._code if self._code is not None else 0

    def finish(self, code: int) -> None:
        self._code = code


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def make() -> tuple[MissionSupervisor, list[FakeProcess], FakeClock]:
    spawned: list[FakeProcess] = []
    clock = FakeClock()

    def launch(_command: list[str], _log_path: str) -> FakeProcess:
        process = FakeProcess()
        spawned.append(process)
        return process

    return MissionSupervisor(launch=launch, clock=clock), spawned, clock


def test_starting_records_what_is_running() -> None:
    supervisor, spawned, _clock = make()

    supervisor.start(MissionKind.SCAN, ["python", "-m", "x"], "var/logs/x.log")

    assert len(spawned) == 1
    running = supervisor.running
    assert running is not None
    assert running.kind is MissionKind.SCAN
    assert running.started_at == NOW


def test_a_second_start_while_running_is_refused() -> None:
    """一个游戏窗口，一个鼠标。两个 runner 同时点会互相点坏。"""
    supervisor, spawned, _clock = make()
    supervisor.start(MissionKind.SCAN, ["python", "-m", "x"], "var/logs/x.log")

    supervisor.start(MissionKind.PIRATE, ["python", "-m", "y"], "var/logs/y.log")

    assert len(spawned) == 1
    assert supervisor.running is not None
    assert supervisor.running.kind is MissionKind.SCAN


def test_stopping_terminates_the_process_and_reports_why() -> None:
    supervisor, spawned, _clock = make()
    supervisor.start(MissionKind.SCAN, ["python", "-m", "x"], "var/logs/x.log")

    finished = supervisor.stop(StopReason.PREEMPTED)

    assert spawned[0].terminated
    assert supervisor.running is None
    assert finished is not None
    assert finished.stopped_by is StopReason.PREEMPTED


def test_polling_collects_a_clean_exit() -> None:
    supervisor, spawned, _clock = make()
    supervisor.start(MissionKind.PIRATE, ["python", "-m", "y"], "var/logs/y.log")
    spawned[0].finish(0)

    finished = supervisor.poll()

    assert supervisor.running is None
    assert finished is not None
    assert finished.exit_code == 0
    assert finished.stopped_by is StopReason.SELF


def test_polling_reports_a_crash_so_the_caller_can_count_it() -> None:
    """连续失败要能自停，否则调度循环会在一个坏掉的任务上满速空转。"""
    supervisor, spawned, _clock = make()
    supervisor.start(MissionKind.PIRATE, ["python", "-m", "y"], "var/logs/y.log")
    spawned[0].finish(1)

    finished = supervisor.poll()

    assert finished is not None
    assert finished.exit_code == 1
    assert finished.stopped_by is StopReason.SELF


def test_polling_an_idle_supervisor_is_harmless() -> None:
    supervisor, _spawned, _clock = make()

    assert supervisor.poll() is None
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m pytest tests/unit/application/test_mission_supervisor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'evo_helper.application.mission_supervisor'`

- [ ] **Step 3: 写实现**

新建 `src/evo_helper/application/mission_supervisor.py`：

```python
"""管调度器的子进程：起、停、收退出码。

刻意不碰界面、不碰数据库、不碰 Win32——起停是这里唯一有分支的部分，
摘干净才测得了。照 `tools.scan_console.ScanSupervisor` 的形状，但
**去掉自动续跑**：那是扫描链路的特性，攻击类任务自己重启会连着再派一轮舰队。
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from evo_helper.domain.scheduler import MissionKind


class StopReason(Enum):
    #: 用户点了结束。
    USER = "USER"
    #: 进程自己退出了（正常跑完，或异常）。
    SELF = "SELF"
    #: 扫描被攻击任务顶掉。
    PREEMPTED = "PREEMPTED"
    #: 控制台自己在关闭。
    SHUTDOWN = "SHUTDOWN"
    #: 控制台重启后发现的孤儿。
    UNKNOWN = "UNKNOWN"


class Process(Protocol):
    """`subprocess.Popen` 里这个模块用到的那一小部分。"""

    pid: int

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = ...) -> int: ...


@dataclass(frozen=True)
class RunningMission:
    kind: MissionKind
    command: list[str]
    log_path: str
    pid: int
    started_at: datetime


@dataclass(frozen=True)
class FinishedMission:
    kind: MissionKind
    started_at: datetime
    ended_at: datetime
    exit_code: int | None
    stopped_by: StopReason


@dataclass
class MissionSupervisor:
    """同时只管一个子进程。一个游戏窗口，一个鼠标。"""

    launch: Callable[[list[str], str], Process]
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    _process: Process | None = None
    _running: RunningMission | None = None

    @property
    def running(self) -> RunningMission | None:
        return self._running

    def start(self, kind: MissionKind, command: list[str], log_path: str) -> RunningMission | None:
        """起一个。已经有进程在跑就拒绝——两个 runner 同时点会互相点坏。"""
        if self._running is not None:
            return None
        process = self.launch(command, log_path)
        self._running = RunningMission(
            kind=kind,
            command=list(command),
            log_path=log_path,
            pid=process.pid,
            started_at=self.clock(),
        )
        self._process = process
        return self._running

    def stop(self, reason: StopReason) -> FinishedMission | None:
        """立刻杀。用户口径：不等它跑完手上这一个。"""
        running, process = self._running, self._process
        if running is None or process is None:
            return None
        process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001 - 收不到退出码也不该让调度循环卡住
            pass
        self._running = None
        self._process = None
        return FinishedMission(
            kind=running.kind,
            started_at=running.started_at,
            ended_at=self.clock(),
            exit_code=process.poll(),
            stopped_by=reason,
        )

    def poll(self) -> FinishedMission | None:
        """收退出码。进程还在跑就返回 None。

        **不自动重启**：失败多半是「窗口抢不到前台」或「甩鼠标触发 FAILSAFE」，
        重启只会再来一遍。调用方数连续失败次数，到阈值就把那条链路停掉。
        """
        running, process = self._running, self._process
        if running is None or process is None:
            return None
        code = process.poll()
        if code is None:
            return None
        self._running = None
        self._process = None
        return FinishedMission(
            kind=running.kind,
            started_at=running.started_at,
            ended_at=self.clock(),
            exit_code=code,
            stopped_by=StopReason.SELF,
        )


def spawn(command: list[str], log_path: str) -> Process:
    """真的拉起一个子进程。测试里绝不会走到这里——那会去点真实鼠标。"""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a", encoding="utf-8")
    return subprocess.Popen(  # noqa: S603 - 命令行完全由 domain.missions 构造
        command,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/application/test_mission_supervisor.py -v`
Expected: PASS（6 条）

- [ ] **Step 5: 跑基线并提交**

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m mypy src
git add src/evo_helper/application/mission_supervisor.py tests/unit/application/test_mission_supervisor.py
git commit -m "调度器的子进程管理

照 ScanSupervisor 的形状，但去掉自动续跑——那是扫描链路的特性，
攻击类任务自己重启会连着再派一轮舰队。同时只管一个进程：一个游戏
窗口，一个鼠标。"
```

---

## Task 9: 把调度循环接进 web 服务（单元 E 续）

**Files:**
- Create: `src/evo_helper/application/mission_scheduler.py`
- Modify: `src/evo_helper/web/app.py`（lifespan）
- Test: `tests/unit/application/test_mission_scheduler.py`

- [ ] **Step 1: 写失败的测试**

新建 `tests/unit/application/test_mission_scheduler.py`：

```python
"""调度循环把纯判据、子进程管理和数据库粘在一起的那一层。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import StopReason
from evo_helper.domain.scheduler import MissionKind

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def test_three_crashes_in_a_row_disable_the_task() -> None:
    """没有这条，调度循环会在一个坏掉的任务上变成满速空转的重启循环。"""
    scheduler = MissionScheduler.__new__(MissionScheduler)
    scheduler._failures = {}  # type: ignore[attr-defined]

    for _ in range(2):
        assert scheduler._note_failure(MissionKind.PIRATE) is None  # type: ignore[attr-defined]
    reason = scheduler._note_failure(MissionKind.PIRATE)  # type: ignore[attr-defined]

    assert reason is not None
    assert "3" in reason


def test_a_clean_exit_clears_the_failure_streak() -> None:
    scheduler = MissionScheduler.__new__(MissionScheduler)
    scheduler._failures = {MissionKind.PIRATE: 2}  # type: ignore[attr-defined]

    scheduler._note_success(MissionKind.PIRATE)  # type: ignore[attr-defined]

    assert scheduler._failures.get(MissionKind.PIRATE, 0) == 0  # type: ignore[attr-defined]


def test_a_preempted_scan_is_not_counted_as_a_failure() -> None:
    """抢占是调度器自己干的，不是扫描出了问题。"""
    scheduler = MissionScheduler.__new__(MissionScheduler)
    scheduler._failures = {}  # type: ignore[attr-defined]

    counted = scheduler._is_failure(exit_code=-15, stopped_by=StopReason.PREEMPTED)  # type: ignore[attr-defined]

    assert counted is False


def test_a_nonzero_self_exit_is_a_failure() -> None:
    scheduler = MissionScheduler.__new__(MissionScheduler)
    scheduler._failures = {}  # type: ignore[attr-defined]

    assert scheduler._is_failure(exit_code=1, stopped_by=StopReason.SELF) is True  # type: ignore[attr-defined]
    assert scheduler._is_failure(exit_code=0, stopped_by=StopReason.SELF) is False  # type: ignore[attr-defined]


def test_report_grace_turns_an_overdue_report_into_a_skip() -> None:
    """一份读不出来的战报不得把任务 2 永久卡住。"""
    from evo_helper.application.mission_scheduler import is_report_abandoned

    expected = NOW - timedelta(minutes=45)

    assert is_report_abandoned(expected, now=NOW, grace=timedelta(minutes=30)) is True
    assert is_report_abandoned(expected, now=NOW, grace=timedelta(hours=2)) is False
    # 飞行时间读不到（None）时永远不判缺失——它本来就该立即去收，不是超时。
    assert is_report_abandoned(None, now=NOW, grace=timedelta(minutes=30)) is False
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m pytest tests/unit/application/test_mission_scheduler.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'evo_helper.application.mission_scheduler'`

- [ ] **Step 3: 写实现**

新建 `src/evo_helper/application/mission_scheduler.py`。它负责：把数据库事实读成 `SchedulerFacts`、调 `decide()`、驱动 `MissionSupervisor`、把每次起停写进 `mission_runs`、数连续失败。

```python
"""调度循环：把纯判据、子进程管理和数据库粘起来。

判据在 `domain.scheduler`，进程在 `application.mission_supervisor`，
事实在数据库——这一层只做搬运和记账，不自己发明规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from evo_helper.application.mission_supervisor import (
    FinishedMission,
    MissionSupervisor,
    StopReason,
)
from evo_helper.domain.report_wait import PendingReport, ReportWaitPlanner, WaitAction
from evo_helper.domain.scheduler import (
    Action,
    MissionKind,
    RunningProcess,
    SchedulerFacts,
    TaskSnapshot,
    decide,
)

#: 同一条链路连续异常退出多少次就自动停用。
MAX_CONSECUTIVE_FAILURES = 3


def is_report_abandoned(
    expected_report_at_utc: datetime | None, *, now: datetime, grace: timedelta
) -> bool:
    """过了预计时间再加宽限期仍读不到，就判为「战报缺失」。

    飞行时间读不到（None）时**永远不判缺失**——那种情况本来就该立即去收，
    不是超时。把它算成缺失，等于把一发真打出去的攻击悄悄抹掉。
    """
    if expected_report_at_utc is None:
        return False
    return now - expected_report_at_utc > grace


def reports_due(pending: list[PendingReport], *, now: datetime) -> bool:
    """这条链路现在该不该回去收战报。

    复用 `ReportWaitPlanner`——同一个判据不该有第二份实现。它还顺带带来了
    正确的 NULL 语义：飞行时间未知时立即收取。
    """
    if not pending:
        return False
    return ReportWaitPlanner().plan(pending, now_utc=now).action is WaitAction.COLLECT


@dataclass
class MissionScheduler:
    """常驻调度循环。由 web 服务的后台任务每秒调一次 `tick()`。"""

    supervisor: MissionSupervisor
    _failures: dict[MissionKind, int] | None = None

    def __post_init__(self) -> None:
        if self._failures is None:
            self._failures = {}

    # -- 失败计数 --------------------------------------------------------------

    @staticmethod
    def _is_failure(*, exit_code: int | None, stopped_by: StopReason) -> bool:
        """这次退出算不算这条链路的失败。

        抢占和用户点停都不算——那是调度器和用户干的，不是 runner 出了问题。
        """
        if stopped_by is not StopReason.SELF:
            return False
        return exit_code is not None and exit_code != 0

    def _note_failure(self, kind: MissionKind) -> str | None:
        """记一次失败。到阈值就返回停用原因。"""
        assert self._failures is not None
        count = self._failures.get(kind, 0) + 1
        self._failures[kind] = count
        if count >= MAX_CONSECUTIVE_FAILURES:
            return f"连续 {count} 次异常退出；已自动停用，看日志"
        return None

    def _note_success(self, kind: MissionKind) -> None:
        assert self._failures is not None
        self._failures[kind] = 0
```

`tick()` 的完整实现要接数据库，放在 Task 10 与仓储接线时一起补——本任务只到失败计数与战报判据这一层，它们是能纯测的部分。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/unit/application/test_mission_scheduler.py -v`
Expected: PASS（5 条）

- [ ] **Step 5: 跑基线并提交**

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m mypy src
git add src/evo_helper/application/mission_scheduler.py tests/unit/application/test_mission_scheduler.py
git commit -m "调度循环的失败计数与战报判据

连续 3 次异常退出自动停用——没有这条，调度循环会在一个坏掉的任务上
满速空转。抢占和用户点停不算失败。战报是否该收复用 ReportWaitPlanner，
不写第二份判据；飞行时间未知时不判缺失，那种情况本来就该立即去收。"
```

---

# 第三段：页面

## Task 10: 调度器的 API（单元 F，依赖 8–9）

**Files:**
- Modify: `src/evo_helper/web/schemas.py`、`src/evo_helper/web/persistent_service.py`、`src/evo_helper/web/app.py`
- Test: `tests/integration/api/test_scheduler_api.py`

- [ ] **Step 1: 读现有约定**

Run: `sed -n '1,60p' src/evo_helper/web/schemas.py`
Run: `grep -n "@app.post\|@app.patch" src/evo_helper/web/app.py | head -20`

照现有端点的写法（`Depends(get_service)`、`response_model`、异常类）来加新端点，不要另起一套。

- [ ] **Step 2: 写失败的测试**

新建 `tests/integration/api/test_scheduler_api.py`：

```python
"""调度台的 API。

写请求只有同源校验（局域网内浏览器天然同源），所以这里只测行为，
不测鉴权——那是 `web/security.py` 的事。
"""

from __future__ import annotations


def test_the_scheduler_starts_stopped(client) -> None:  # type: ignore[no-untyped-def]
    """控制台重启后一律停在「已停止」。重启多半意味着出了事。"""
    body = client.get("/api/scheduler").json()

    assert body["running"] is False
    assert body["current"] is None


def test_the_three_tasks_are_listed_in_priority_order(client) -> None:  # type: ignore[no-untyped-def]
    body = client.get("/api/scheduler").json()

    kinds = [task["kind"] for task in body["tasks"]]
    assert sorted(kinds) == ["BOT", "PIRATE", "SCAN"]
    priorities = [task["priority"] for task in body["tasks"]]
    assert priorities == sorted(priorities)


def test_priority_can_be_reordered(client) -> None:  # type: ignore[no-untyped-def]
    client.patch("/api/missions/SCAN", json={"priority": 0})

    body = client.get("/api/scheduler").json()
    first = body["tasks"][0]
    assert first["kind"] == "SCAN"


def test_a_bot_range_with_no_recorded_bots_is_refused(client) -> None:  # type: ignore[no-untyped-def]
    """拉起一个必然空转的 runner 没有意义，早一步告诉用户。"""
    response = client.patch(
        "/api/missions/BOT",
        json={"enabled": True, "params": {"galaxy": 9, "first_system": 1, "last_system": 2}},
    )

    assert response.status_code == 400
    assert "没有已记录的 bot" in response.json()["detail"]


def test_a_non_positive_pirate_radius_is_refused(client) -> None:  # type: ignore[no-untyped-def]
    response = client.patch("/api/missions/PIRATE", json={"params": {"radius": 0}})

    assert response.status_code == 400


def test_starting_and_stopping_flips_the_flag(client) -> None:  # type: ignore[no-untyped-def]
    client.post("/api/scheduler/start")
    assert client.get("/api/scheduler").json()["running"] is True

    client.post("/api/scheduler/stop")
    assert client.get("/api/scheduler").json()["running"] is False
```

- [ ] **Step 3: 跑测试确认它失败**

Run: `python -m pytest tests/integration/api/test_scheduler_api.py -v`
Expected: FAIL — 404，路由还不存在

- [ ] **Step 4: 加 schemas**

在 `src/evo_helper/web/schemas.py` 追加：

```python
class MissionTaskOut(BaseModel):
    kind: str
    enabled: bool
    priority: int
    params: dict[str, int]
    status: str
    detail: str
    disabled_reason: str | None


class MissionTaskPatch(BaseModel):
    enabled: bool | None = None
    priority: int | None = None
    params: dict[str, int] | None = None


class CurrentMissionOut(BaseModel):
    kind: str
    started_at_utc: datetime
    log_path: str


class SchedulerOut(BaseModel):
    running: bool
    tasks: list[MissionTaskOut]
    current: CurrentMissionOut | None
    orphan_pid: int | None
```

- [ ] **Step 5: 加服务方法**

在 `src/evo_helper/web/persistent_service.py` 的 `ApplicationService` 里加 `scheduler_view()`、`patch_mission(kind, patch)`、`start_scheduler()`、`stop_scheduler()`、`restart_bot_round()`、`force_kill_orphan()`。参数校验一律走 `domain.missions` 的 `MissionParamError`，在 `app.py` 里翻成 400。

- [ ] **Step 6: 加路由**

在 `src/evo_helper/web/app.py` 的 runs 段之后加：

```python
    # ---- scheduler -------------------------------------------------------

    @app.get("/api/scheduler", response_model=SchedulerOut)
    async def scheduler_state(
        service: ApplicationService = Depends(get_service),
    ) -> SchedulerOut:
        return service.scheduler_view()

    @app.post("/api/scheduler/start", response_model=SchedulerOut)
    async def scheduler_start(
        service: ApplicationService = Depends(get_service),
    ) -> SchedulerOut:
        return service.start_scheduler()

    @app.post("/api/scheduler/stop", response_model=SchedulerOut)
    async def scheduler_stop(
        service: ApplicationService = Depends(get_service),
    ) -> SchedulerOut:
        return service.stop_scheduler()

    @app.patch("/api/missions/{kind}", response_model=MissionTaskOut)
    async def patch_mission(
        kind: str,
        payload: MissionTaskPatch,
        service: ApplicationService = Depends(get_service),
    ) -> MissionTaskOut:
        return service.patch_mission(kind, payload)

    @app.post("/api/missions/BOT/new-round", response_model=MissionTaskOut)
    async def restart_bot_round(
        service: ApplicationService = Depends(get_service),
    ) -> MissionTaskOut:
        return service.restart_bot_round()

    @app.post("/api/scheduler/force-kill", response_model=SchedulerOut)
    async def force_kill(
        service: ApplicationService = Depends(get_service),
    ) -> SchedulerOut:
        return service.force_kill_orphan()
```

- [ ] **Step 7: 跑测试确认通过**

Run: `python -m pytest tests/integration/api/test_scheduler_api.py -v`
Expected: PASS（6 条）

- [ ] **Step 8: 跑基线并提交**

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m mypy src
git add src/evo_helper/web/
git add tests/integration/api/test_scheduler_api.py
git commit -m "调度台的 API

参数校验走 domain.missions 的 MissionParamError，翻成 400——范围内
没有已记录 bot 时早一步拒绝，别拉起一个必然空转的 runner。
调度器开关不持久化，重启后一律停在已停止。"
```

---

## Task 11: 调度台页面（单元 G，依赖 10）

**Files:**
- Modify: `src/evo_helper/web/templates/missions.html`（整页重做）
- Modify: `src/evo_helper/web/app.py`（`missions_page` 的 context）
- Test: `tests/e2e/test_missions_console.py`

- [ ] **Step 1: 写失败的测试**

新建 `tests/e2e/test_missions_console.py`：

```python
"""调度台页面。"""

from __future__ import annotations


def test_the_page_lists_the_three_tasks(client) -> None:  # type: ignore[no-untyped-def]
    html = client.get("/missions").text

    assert "侦查+攻击海盗" in html
    assert "扫描+攻击 bot" in html
    assert "扫描全星系 bot" in html


def test_the_old_plan_form_is_gone(client) -> None:  # type: ignore[no-untyped-def]
    """那个表单产出的计划行没有任何 runner 会读。

    填了没人读的表单，比没有表单更害人。
    """
    html = client.get("/missions").text

    assert "新建扫描任务" not in html
    assert "扫描区段" not in html


def test_the_time_window_chip_is_gone(client) -> None:  # type: ignore[no-untyped-def]
    """定时没了，这个 chip 就是句谎话。"""
    html = client.get("/missions").text

    assert "时间窗口 UTC+8" not in html


def test_the_page_offers_start_and_stop(client) -> None:  # type: ignore[no-untyped-def]
    html = client.get("/missions").text

    assert "/api/scheduler/start" in html
    assert "/api/scheduler/stop" in html
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `python -m pytest tests/e2e/test_missions_console.py -v`
Expected: FAIL — 旧页面还在，「新建扫描任务」仍在 HTML 里

- [ ] **Step 3: 重做模板**

把 `src/evo_helper/web/templates/missions.html` 整个替换。保留 `{% extends "base.html" %}` 与现有 CSS class 约定（`panel` / `grid-12` / `chip` / `tile` / `btn`）。结构：

1. `topbar_extra`：调度器状态 chip（去掉时间窗口那个）。
2. 孤儿红条（`orphan_pid` 非空时显示，带「强制结束」按钮 POST `/api/scheduler/force-kill`）。
3. 「当前任务」面板：种类、已运行时长、日志路径、结束按钮。
4. 「任务链路」面板：三行可拖拽（HTML5 `draggable` + `dragover`/`drop`），每行含复选框、名称、参数输入框、状态 chip。放手时 PATCH 各行的新 `priority`。
5. 「历史」面板：`mission_runs` 表格（种类 / 参数 / 起止 / 时长 / 结束方式 / 退出码）。
6. 轮询：`setInterval` 每 2 秒 GET `/api/scheduler` 刷新状态区（不刷整页——正在输入的参数框会被清掉）。

- [ ] **Step 4: 改 `missions_page` 的 context**

把 `src/evo_helper/web/app.py` 里 `missions_page` 的 context 换成调度台需要的：

```python
                "active": "missions",
                "scheduler": service.scheduler_view(),
                "runs": service.list_mission_runs(limit=50),
```

删掉 `plans` / `default_preset` / `default_preset_signature` 这些只服务旧表单的键。

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/e2e/test_missions_console.py -v`
Expected: PASS（4 条）

- [ ] **Step 6: 实机看一眼**

```bash
python -m evo_helper.web.runtime
```

在浏览器打开 `http://127.0.0.1:8770/missions`，确认：三行能拖动且松手后次序保存；bot 的系号区间旁显示「该范围内已记录 bot：N 个」；海盗半径旁显示覆盖区间；**先不要点「开始」**——那会真的拉起 runner 去点鼠标。

- [ ] **Step 7: 跑基线并提交**

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m mypy src
git add src/evo_helper/web/templates/missions.html src/evo_helper/web/app.py tests/e2e/test_missions_console.py
git commit -m "missions 页改成调度台

三行可拖拽定优先级，复选框控制参与，参数就地编辑。撤掉「新建扫描
任务」表单——它产出的计划行没有任何 runner 会读，填了没人读的表单比
没有表单更害人。时间窗口 chip 一并去掉：定时没了它就是句谎话。
状态区单独轮询，不刷整页，免得清掉正在输入的参数。"
```

---

## Task 12: 变更记录与交接文档

**Files:**
- Create: `.changes/28-mission-scheduler.md`
- Modify: `TODO-交接.md`

- [ ] **Step 1: 照模板写变更记录**

Run: `cat .changes/template.md`

按模板新建 `.changes/28-mission-scheduler.md`，重点写清三件事：调度器的航线估算是乐观的（权威闸门仍在 runner）、bot 改成「派出即退出」的原因、`target_kind` 那个 bug 的影响面（会污染 32/天配额）。

- [ ] **Step 2: 更新交接文档**

把 `TODO-交接.md` 里需求 4 的状态从「未开始」改成「已完成」，并删掉第五节里那条已经作废的建议（「`window_start`/`window_end` 两列先别删」——本次改动根本没碰那两列）。

- [ ] **Step 3: 提交**

```bash
git add .changes/28-mission-scheduler.md TODO-交接.md
git commit -m "变更记录与交接文档"
```

---

## 自查记录

写完后对照规格逐节核了一遍，修掉的问题：

- **Task 3 的依赖**：`bot_loop` 用到的 `bot_dispatch_facts` / `mark_bot_target_skipped` 由 Task 7 提供，而两者在同一波次并行。已在 Task 3 Step 9 写明：单元测试必须绿，集成层的 `AttributeError` 是预期的，合流后再一起验。
- **`GUARD_STATUS_SKIPPED` 的归属**：`domain.bot_round.DispatchFact.skipped` 与仓储里的 `guard_status` 是同一件事的两个表示，Task 7 Step 4 明确了转换点。
- **Task 9 的边界**：`tick()` 要接数据库，本任务只做能纯测的两层（失败计数、战报判据），完整接线在 Task 10 与服务层一起落。这是有意的切分，不是遗漏。
- **未覆盖项**：规格 §五 提到的「lifespan 关闭时主动 terminate 子进程」落在 Task 10 的服务层改动里，实施时记得在 `create_persistent_app` 的 lifespan 里加 `supervisor.stop(StopReason.SHUTDOWN)`。
