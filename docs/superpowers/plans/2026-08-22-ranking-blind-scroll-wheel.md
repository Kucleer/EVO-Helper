# 军力榜盲滚改滚轮 —— 实现计划

> **给执行者：** 按 `superpowers:executing-plans` 逐条执行。步骤用 `- [ ]` 勾选跟踪。
> **默认派子 agent 执行**（用户口径 2026-08-22，见 `AGENTS.md` 5.0）。
> 每个子任务提示词里必须复述硬约束：不连生产库、不跑 `alembic upgrade`、不启动游戏、不动鼠标。

**Goal:** 盲滚段从「慢拖 70 屏 × 每屏等 2 秒（294.6 秒）」改成「滚轮单格连拨 + 末尾等一次滑行（约 11 秒）」，口径从「屏」统一到「行」，新增运维旋钮 `blind_scroll_rows`。

**Architecture:** 改在**循环这一层**而不是 `scroll_blind()` 内部——只换内部会把 70 次 × 2 秒的等待原样留下。`game` 层拿到新原语 `wheel_notch()`（单格 `dwData=±120`），格数与 16ms 间隔的循环放在 `game/ranking_nav.spin_blind()`，同 `_slow_drag` 分步的理由。检测段与采集段**一律不动**。

**Tech Stack:** Python 3.12 / pyautogui / SQLAlchemy 2 + Alembic / FastAPI + Jinja2 / pytest。

**依据：** [`docs/superpowers/specs/2026-08-22-ranking-blind-scroll-wheel-design.md`](../specs/2026-08-22-ranking-blind-scroll-wheel-design.md)

**关键既有常量（都在 `game/ranking_ui.py`）：** `FIRST_BOT_RANK = 587`（⚠️ **不是安全边界**——那段是玩家伪装的 bot，见 `AGENTS.md` 4.8；本计划不拿它拦任何东西）、`ROWS_PER_SCROLL = 8.3`、`BLIND_SCROLL_MARGIN = 10`(屏)、`BLIND_SCROLL_SAMPLES = 5`、`BLIND_SCROLLS = 40`、`BOT_DETECTION_BUDGET_SCROLLS = 60`。

---

## Task 1: 滚轮标定常量

**Files:**
- Modify: `src/evo_helper/game/ranking_ui.py`（`SCROLL_SETTLE_WAIT_S` 附近，约 442–466 行）
- Test: `tests/unit/game/test_ranking_wheel_constants.py`

- [ ] **Step 1: 写失败的测试**

```python
"""滚轮标定常量。这些数一旦漂了，盲滚距离会静默变化。"""

from evo_helper.game import ranking_ui


def test_one_notch_is_the_windows_standard_delta() -> None:
    # ⚠️ 一格 = 120。发不足一格等于没发（实测 dwData=-1 时 80 格只走 0-3 行）；
    # 发一个大 delta 会被游戏封顶（实测 800 格只走 14px）。
    assert ranking_ui.WHEEL_DELTA == 120


def test_notch_gap_matches_the_measured_human_cadence() -> None:
    assert ranking_ui.WHEEL_GAP_S == 0.016


def test_rows_per_notch_is_the_measured_calibration() -> None:
    assert ranking_ui.ROWS_PER_NOTCH == 1.08


def test_blind_rows_default_is_the_user_configured_value() -> None:
    assert ranking_ui.BLIND_SCROLL_ROWS == 700


def test_glide_settle_covers_the_measured_inertia() -> None:
    # 实测滑行 1.6-2.3 秒才停；取 2.5 留余量。
    assert ranking_ui.GLIDE_SETTLE_S >= 2.3


def test_new_constants_are_exported() -> None:
    for name in (
        "WHEEL_DELTA",
        "WHEEL_GAP_S",
        "ROWS_PER_NOTCH",
        "GLIDE_SETTLE_S",
        "BLIND_SCROLL_ROWS",
    ):
        assert name in ranking_ui.__all__
```

- [ ] **Step 2: 跑，确认失败**

Run: `python -m pytest tests/unit/game/test_ranking_wheel_constants.py -q`
Expected: FAIL，`AttributeError: module ... has no attribute 'WHEEL_DELTA'`

- [ ] **Step 3: 加常量**

在 `SCROLL_SETTLE_WAIT_S` 那一段之后插入：

```python
# -- 滚轮盲滚（2026-08-22 实测）------------------------------------------------

#: 一格滚轮的 `dwData`。**这是 Windows 的标准值，不是偏好项。**
#:
#: ⚠️ 两个方向都会静默失败：
#: - **发不足一格**等于没发。`pyautogui.scroll(n)` 在 Windows 上把 `n` 原样当
#:   `dwData`，**不乘 120**——`scroll(-1)` 只是 1/120 格，实测 80 次只走 0–3 行。
#:   `docs/军力榜翻页-滚轮实测.md` 那句「1 格 = 1 像素」记的就是这个，不是滚轮的幅度。
#: - **一个事件发大 delta 会被游戏封顶**：实测 100/400/800 格都只走约 14px。
#:
#: 底层鼠标钩子实测：助手发 `scroll(-1)` 钩子看到 `delta=-1`，用户手动是 `-120`。
WHEEL_DELTA = 120

#: 两格之间隔多久。实测用户手动连滚的间隔中位数就是 16ms，而这个数**决定成败**：
#: 游戏做的是**速度惯性滚动**，拉到 117ms/格（`pyautogui.PAUSE` 的默认值）
#: 就攒不起动量，80 格只走 2 行。
WHEEL_GAP_S = 0.016

#: 一格推进多少行。**标定常量，不是运维旋钮。**
#:
#: ⚠️ **只有 2 个样本**（40 格→44 行、80 格→85 行；2026-08-22 单机单次）。
#: 偏大就会拨过 bot 起点，而那是静默故障。每轮实测值记进 `system_log`
#: （见 `tools.ranking_scan`），好在事后回答「这个标定还成不成立」。
ROWS_PER_NOTCH = 1.08

#: 拨完之后等滑行停下来。实测惯性滑行 1.6–2.3 秒，取 2.5 留余量。
#:
#: **不等的代价是检测段在移动中的画面上读行**——读出来的名字会横跨两行。
GLIDE_SETTLE_S = 2.5

#: 盲滚多少行（攻击配置页上那个框留空时的默认值）。
#:
#: 用户口径（2026-08-22）：「默认行按 700 行配置，默认值我会最终配置」。
#:
#: ⚠️ **不要拿 `FIRST_BOT_RANK`(587) 当上界去拦。** 用户口径（2026-08-22）：
#: 那个「bot 起点」是**玩家改名伪装**出来的，不是真 bot——判据只看名字前缀，
#: 改名的真人一样命中。真 bot 区在更后面，所以 700 不越界，
#: 而 `BLIND_SCROLLS` 注释里「40×12=480 < 587」那套推理的前提已不成立。
BLIND_SCROLL_ROWS = 700
```

并把五个名字加进 `__all__`（该列表按字母序）。

- [ ] **Step 4: 跑，确认通过**

Run: `python -m pytest tests/unit/game/test_ranking_wheel_constants.py -q`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add src/evo_helper/game/ranking_ui.py tests/unit/game/test_ranking_wheel_constants.py
git commit -m "feat(军力榜盲滚): 滚轮标定常量，含「发不足一格/发大 delta 都静默失败」的实测依据"
```

---

## Task 2: `game` 层的滚轮原语与 `spin_blind`

**Files:**
- Modify: `src/evo_helper/game/ranking_nav.py`（`RankingDriver` 协议约 85–102 行；`scroll_blind` 约 336–350 行）
- Test: `tests/unit/game/test_ranking_spin_blind.py`

- [ ] **Step 1: 写失败的测试**

```python
"""盲滚：行 → 格的换算，以及「一次点击都不发」。"""

from dataclasses import dataclass, field

from evo_helper.game.ranking_nav import RankingNavigator, SpinResult
from evo_helper.game.ranking_ui import ROWS_PER_NOTCH


@dataclass
class FakeDriver:
    notches: int = 0
    waits: list[float] = field(default_factory=list)
    clicks: list[tuple[int, int]] = field(default_factory=list)
    presses: int = 0

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y))

    def press(self, x: int, y: int, *, label: str = "") -> None:
        self.presses += 1

    def move_to(self, x: int, y: int) -> None: ...

    def release(self) -> None: ...

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)

    def wheel_notch(self) -> None:
        self.notches += 1


def _nav(driver: FakeDriver) -> RankingNavigator:
    return RankingNavigator(
        driver=driver,
        read_labels=lambda: [],
        read_rows=lambda: [],
        row_has_score=lambda row: True,
        say=lambda _m: None,
    )


def test_rows_convert_to_notches_by_the_calibration() -> None:
    driver = FakeDriver()
    result = _nav(driver).spin_blind(rows=108)
    assert driver.notches == round(108 / ROWS_PER_NOTCH)
    assert result.notches == driver.notches
    assert result.rows_requested == 108


def test_zero_rows_sends_nothing() -> None:
    # 0 是最保守的合法取值：「一格都别拨」。
    driver = FakeDriver()
    result = _nav(driver).spin_blind(rows=0)
    assert driver.notches == 0
    assert result.notches == 0
    assert driver.waits == []


def test_negative_rows_is_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        _nav(FakeDriver()).spin_blind(rows=-1)


def test_spin_waits_once_for_the_glide_not_once_per_notch() -> None:
    # ⚠️ 这条是整个改动的要害：每格都等 = 白改。
    from evo_helper.game.ranking_ui import GLIDE_SETTLE_S

    driver = FakeDriver()
    _nav(driver).spin_blind(rows=500)
    assert driver.waits.count(GLIDE_SETTLE_S) == 1
    assert len([w for w in driver.waits if w >= 1.0]) == 1


def test_spin_never_clicks_or_presses() -> None:
    driver = FakeDriver()
    _nav(driver).spin_blind(rows=500)
    assert driver.clicks == []
    assert driver.presses == 0
```

- [ ] **Step 2: 跑，确认失败**

Run: `python -m pytest tests/unit/game/test_ranking_spin_blind.py -q`
Expected: FAIL，`ImportError: cannot import name 'SpinResult'`

- [ ] **Step 3: 实现**

`RankingDriver` 协议加一个原语（放在 `release` 之后）：

```python
    def wheel_notch(self) -> None:
        """往下滚**一格**（`dwData = -WHEEL_DELTA`）。

        ⚠️ 协议上只有「一格」这一种粒度，是**有意的**：
        允许传格数的话，实现里迟早会把 N 格合成一个大事件发出去，
        而那会被游戏封顶（实测 800 格只走 14px），且封顶是静默的。
        密度由 `RankingNavigator.spin_blind` 控制。
        """
        ...
```

模块顶部 import 补 `GLIDE_SETTLE_S`、`ROWS_PER_NOTCH`、`WHEEL_GAP_S`。

在 `scroll_blind` 之后新增：

```python
@dataclass(frozen=True, slots=True)
class SpinResult:
    """一趟盲滚的账。`rows_measured` 由调用方填（这一层不读屏）。"""

    rows_requested: int
    notches: int
    spin_seconds: float


    def spin_blind(self, *, rows: int) -> SpinResult:
        """连续拨滚轮走过 `rows` 行，**中间不读也不判**，末尾统一等一次滑行。

        ⚠️ **等待只有末尾那一次**，不是每格一次。`scroll_blind` 那条路每屏都
        `wait(SCROLL_SETTLE_WAIT_S)`，70 屏就是 140 秒纯等待——盲滚改滚轮的收益
        全部来自把这些等待合并成一次。

        ⚠️ **不许把 N 格合成一个大事件。** 游戏对单个事件的幅度封顶
        （实测 800 格只走 14px），而封顶静默；动量靠的是密集的独立事件。
        """
        if rows < 0:
            raise ValueError("rows must not be negative")
        notches = round(rows / ROWS_PER_NOTCH)
        started = time.monotonic()
        for _ in range(notches):
            self.driver.wheel_notch()
            self.driver.wait(WHEEL_GAP_S)
        spin_seconds = time.monotonic() - started
        if notches:
            self.driver.wait(GLIDE_SETTLE_S)
        return SpinResult(rows_requested=rows, notches=notches, spin_seconds=spin_seconds)
```

模块顶部补 `import time` 与 `from dataclasses import dataclass`（已有则不重复）。

- [ ] **Step 4: 跑，确认通过**

Run: `python -m pytest tests/unit/game/test_ranking_spin_blind.py -q`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add src/evo_helper/game/ranking_nav.py tests/unit/game/test_ranking_spin_blind.py
git commit -m "feat(军力榜盲滚): game 层加 wheel_notch 原语与 spin_blind，等待合并成末尾一次"
```

---

## Task 3: 驱动实现 `wheel_notch`

**Files:**
- Modify: `src/evo_helper/tools/scan_coordinates.py`（`LiveDriver` 约 641–706 行；`SlowDragDriver` 约 710 行起）
- Test: `tests/unit/tools/test_live_driver_wheel.py`

- [ ] **Step 1: 写失败的测试**

```python
"""驱动发出去的滚轮事件必须是「单格」，而且 pyautogui 的 PAUSE 必须被关掉。"""

import sys
import types

import pytest


@pytest.fixture
def fake_pyautogui(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("pyautogui")
    module.PAUSE = 0.1  # type: ignore[attr-defined]
    module.FAILSAFE = True  # type: ignore[attr-defined]
    module.scrolled = []  # type: ignore[attr-defined]
    module.scroll = lambda clicks: module.scrolled.append(clicks)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyautogui", module)
    return module


def test_wheel_notch_sends_exactly_one_standard_notch(fake_pyautogui) -> None:
    from evo_helper.game.ranking_ui import WHEEL_DELTA
    from evo_helper.tools.scan_coordinates import SlowDragDriver

    driver = SlowDragDriver(_stub_live_driver())
    driver.wheel_notch()
    assert fake_pyautogui.scrolled == [-WHEEL_DELTA]


def test_wheel_notch_disables_the_global_pause(fake_pyautogui) -> None:
    # ⚠️ PAUSE=0.1 会把 16ms 的间隔撑成 117ms，动量攒不起来——
    # 症状是「拨了但没走」，和发不足一格一模一样。
    from evo_helper.tools.scan_coordinates import SlowDragDriver

    SlowDragDriver(_stub_live_driver()).wheel_notch()
    assert fake_pyautogui.PAUSE == 0


def test_wheel_notch_keeps_failsafe_on(fake_pyautogui) -> None:
    from evo_helper.tools.scan_coordinates import SlowDragDriver

    SlowDragDriver(_stub_live_driver()).wheel_notch()
    assert fake_pyautogui.FAILSAFE is True
```

`_stub_live_driver()` 在同文件里定义一个最小替身，只需 `focus()` 与 `origin()`。

- [ ] **Step 2: 跑，确认失败**

Run: `python -m pytest tests/unit/tools/test_live_driver_wheel.py -q`
Expected: FAIL，`AttributeError: 'SlowDragDriver' object has no attribute 'wheel_notch'`

- [ ] **Step 3: 实现**

`SlowDragDriver` 加：

```python
    def wheel_notch(self) -> None:
        """往下滚一格。

        ⚠️ **`pyautogui.scroll(n)` 在 Windows 上把 `n` 原样当 `dwData`，不乘 120。**
        所以这里传的是 `-WHEEL_DELTA`(-120) 而不是 `-1`——传 `-1` 只是 1/120 格，
        实测 80 次只走 0–3 行，而它看起来完全正常（事件发出去了、也被钩子收到了）。

        ⚠️ **`PAUSE` 必须置 0。** 它默认 0.1 秒，会把 `WHEEL_GAP_S`(16ms) 撑成
        117ms/格，动量攒不起来，80 格只走 2 行。
        FAILSAFE 不动——急停照常。
        """
        import pyautogui

        pyautogui.PAUSE = 0
        pyautogui.scroll(-WHEEL_DELTA)
```

`LiveDriver` 同样加一份委托（若 `SlowDragDriver` 持有 `LiveDriver`，则 `wheel_notch` 直接实现在 `SlowDragDriver` 上即可，`LiveDriver` 不必重复）。文件顶部 import 补 `WHEEL_DELTA`。

- [ ] **Step 4: 跑，确认通过**

Run: `python -m pytest tests/unit/tools/test_live_driver_wheel.py -q`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add src/evo_helper/tools/scan_coordinates.py tests/unit/tools/test_live_driver_wheel.py
git commit -m "feat(军力榜盲滚): 驱动发单格滚轮，并显式关掉 pyautogui 的全局 PAUSE"
```

---

## Task 4: `ranking_scan` 换成行口径

**Files:**
- Modify: `src/evo_helper/tools/ranking_scan.py`（`report_bot_area_reached` 约 298–325 行；`scroll_through_humans` 约 440–530 行；`ScanProgress` 约 332–346 行；`scan()` 里的调用点）
- Test: `tests/unit/tools/test_ranking_human_stretch.py`（改）、`tests/unit/tools/test_ranking_blind_scroll_warning.py`（改）

- [ ] **Step 1: 改测试——盲滚段只调一次 `spin`**

在 `tests/unit/tools/test_ranking_human_stretch.py` 加：

```python
def test_blind_phase_spins_once_instead_of_scrolling_per_screen() -> None:
    spun: list[int] = []
    scrolls = 0

    def scroll() -> None:
        nonlocal scrolls
        scrolls += 1

    stretch = scroll_through_humans(
        scroll=scroll,
        spin=lambda rows: spun.append(rows) or rows,
        read_names=_names_reaching_bots_after(3),
        wait=lambda _s: None,
        blind_rows=500,
        detection_budget=10,
        say_line=lambda _m: None,
    )
    assert spun == [500]          # 一次，不是 70 次
    assert scrolls == 3           # 只有检测段在慢拖
    assert stretch.blind_rows == 500
```

- [ ] **Step 2: 跑，确认失败**

Run: `python -m pytest tests/unit/tools/test_ranking_human_stretch.py -q`
Expected: FAIL，`TypeError: scroll_through_humans() got an unexpected keyword argument 'spin'`

- [ ] **Step 3: 改实现**

`scroll_through_humans` 签名把 `blind_scrolls: int` 换成 `blind_rows: int`，新增
`spin: Callable[[int], int]`；盲滚那一段：

```python
    progress.stage = ScanStage.BLIND
    progress.blind_rows = blind_rows
    rows_spun = spin(blind_rows) if blind_rows else 0
    progress.human_rows = rows_spun
    progress.stage = ScanStage.DETECTING
    if blind_rows:
        say_line(f"盲滚 {blind_rows} 行（那一段必定还是真人），开始检测 bot")
```

检测段的 `scrolled` 仍按屏计，但对外的账改成行：

```python
    def rows_now() -> int:
        """到此刻为止走了多少行 = 盲滚的行 + 检测段的屏 × 每屏行数。"""
        return rows_spun + round(scrolled * ROWS_PER_SCROLL)
```

`HumanStretch` 增加 `rows: int` 字段（保留 `scrolled` 表示检测段翻了几屏）。

`report_bot_area_reached` 改成行：

```python
def report_bot_area_reached(rows: int, *, blind_rows: int) -> None:
    """实测到达 bot 区用了多少行，并在余量不足时告警。

    ⚠️ **余量是行，不再是屏**，少一处 `ROWS_PER_SCROLL` 换算。
    ⚠️ 这里比的是「实测到达 bot 区的行数」与「盲滚了多少行」，**不引入 `FIRST_BOT_RANK`**
    ——那个 587 是玩家伪装污染出来的（`AGENTS.md` 4.8），不能当边界。
    """
    slack = rows - blind_rows
    say(bot_area_reached_message(rows))
    if slack >= BLIND_SCROLL_MARGIN_ROWS:
        return
    warn(
        f"⚠️ 盲滚余量告急：本趟实测 {rows} 行到达 bot 区，而盲滚了 {blind_rows} 行，"
        f"余量只剩 {slack} 行（应有 {BLIND_SCROLL_MARGIN_ROWS} 行）。"
        "再漂一点盲滚就会滚过 bot 起点，把榜首那批军力最高的 bot 整段跳过去，"
        "而采回来的数只会静悄悄少一截。请检查攻击配置页上的盲滚行数是不是填得太大。"
    )
```

`scan()` 里把 `nav.scroll_blind` 那条路换成 `lambda rows: nav.spin_blind(rows=rows).notches`，
并把 `spin` 的返回换算回行记账。

- [ ] **Step 4: 跑，确认通过**

Run: `python -m pytest tests/unit/tools/ -q`
Expected: all passed

- [ ] **Step 5: 提交**

```bash
git add src/evo_helper/tools/ranking_scan.py tests/unit/tools/
git commit -m "feat(军力榜盲滚): 采集器改行口径，盲滚一次连拨、检测段照旧慢拖"
```

---

## Task 5: 自标定与日志正文改行

**Files:**
- Modify: `src/evo_helper/domain/ranking.py`（`bot_area_scrolls` 约 295–298 行；`calibrated_blind_scrolls` 约 301 行起；`BOT_AREA_REACHED_PREFIX` 与正则）
- Modify: `src/evo_helper/game/ranking_ui.py`（加 `BLIND_SCROLL_MARGIN_ROWS`）
- Test: `tests/unit/domain/test_ranking_calibration.py`（改/新增）

- [ ] **Step 1: 写失败的测试**

```python
def test_calibrated_blind_rows_takes_min_minus_margin() -> None:
    assert calibrated_blind_rows([560, 548, 591, 570, 566], sample_size=5, margin=73) == 475


def test_not_enough_samples_returns_none() -> None:
    assert calibrated_blind_rows([560, 548], sample_size=5, margin=73) is None


def test_bot_area_rows_parses_the_new_message() -> None:
    assert bot_area_rows(bot_area_reached_message(566)) == 566


def test_old_screen_message_is_not_misread_as_rows() -> None:
    # ⚠️ 库里存着一整年「翻了 N 屏到达 bot 区」的历史。把 78 屏当成 78 行会让
    # 自标定给出一个荒谬的小值，而小值是**安全**的——但必须是有意的，不是撞上的。
    assert bot_area_rows("翻了 78 屏到达 bot 区") is None
```

- [ ] **Step 2: 跑，确认失败**

Run: `python -m pytest tests/unit/domain/test_ranking_calibration.py -q`
Expected: FAIL，`ImportError: cannot import name 'calibrated_blind_rows'`

- [ ] **Step 3: 实现**

- 新增 `BLIND_SCROLL_MARGIN_ROWS = 73`（= 原 10 屏 × `ROWS_PER_SCROLL` 8.3，取整），并在注释里写明它由 10 屏换算而来。
- `bot_area_reached_message(rows)` 改成「翻了 N 行到达 bot 区」，新增 `bot_area_rows(message)` 只匹配**行**那一版正文；**旧的屏版正文保留一个只读解析器不再参与标定**（见测试第三条）。
- `calibrated_blind_rows(measurements, *, sample_size, margin)`：与原 `calibrated_blind_scrolls` 同形，仅单位变行。原函数**删除**（没有第二个调用点）。

- [ ] **Step 4: 跑，确认通过**

Run: `python -m pytest tests/unit/domain/ -q`
Expected: all passed

- [ ] **Step 5: 提交**

```bash
git add src/evo_helper/domain/ranking.py src/evo_helper/game/ranking_ui.py tests/unit/domain/
git commit -m "feat(军力榜盲滚): 自标定与到达 bot 区的日志正文改行；旧的屏版正文不再参与标定"
```

---

## Task 6: 配置列与迁移

**Files:**
- Modify: `src/evo_helper/storage/models.py`（`blind_scrolls` 那一列之后，约 844 行）
- Create: `alembic/versions/<rev>_blind_scroll_rows.py`
- Test: `tests/integration/storage/test_blind_scroll_rows_migration.py`

- [ ] **Step 1: 写失败的测试**（照 `tests/integration/storage/test_bot_target_unreadable_migration.py` 的形状：升级后列存在且存量行为 NULL，降级后列消失）

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 加列 + 迁移**

```python
    #: 盲滚多少**行**（`game.ranking_ui.BLIND_SCROLL_ROWS` = 700 是留空时的默认值）。
    #:
    #: **可空、不给 server_default**：NULL = 「跟着代码默认走」。先例是
    #: `blind_scrolls`——给了默认值就分不开「没配」和「恰好配成当前默认」。
    #:
    #: ⚠️ `blind_scrolls`（屏）**刻意保留不删**：置空本列即退回慢拖那条路，
    #: 是这次改动的一键回滚，不需要改代码。
    blind_scroll_rows: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
```

迁移 `down_revision` 挂在当前 head（执行时用 `python -m alembic heads` 确认，**必须单 head**）。

- [ ] **Step 4: 跑，确认通过**

Run: `python -m pytest tests/integration/storage/ -q`

- [ ] **Step 5: 提交**

```bash
git add src/evo_helper/storage/models.py alembic/versions/ tests/integration/storage/
git commit -m "feat(军力榜盲滚): 新增 blind_scroll_rows 列与迁移；blind_scrolls 保留作回滚"
```

---

## Task 7: 接口与设置页

**Files:**
- Modify: `src/evo_helper/web/schemas.py`（`blind_scrolls` 之后，约 263 行）
- Modify: `src/evo_helper/web/service.py` / `app.py`（`/api/attack-config` 的读写两侧）
- Modify: `src/evo_helper/web/templates/settings.html`（第 15–33 行那个 panel 之后新增一个）
- Test: `tests/integration/api/test_scheduler_api.py`（把新旋钮加进「所有旋钮一起存一起读」那张表）

- [ ] **Step 1: 写失败的测试**——一次 `PUT` 带上 `blind_scroll_rows`，`GET` 原样读回；`3.5` / `"很多"` / `true` 一律 422；`0` 与 `700` 都收下；页面 `GET /settings` 200 且正文含 `blind-scroll-rows`。

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 实现**

`settings.html` 新增一节。⚠️ **不做「越界」判断**（`FIRST_BOT_RANK` 不是边界，见 `AGENTS.md` 4.8），只做行↔秒换算：

```html
<section class="panel" aria-labelledby="blind-rows-head">
  <h2 id="blind-rows-head">军力榜盲滚行数</h2>
  <p class="muted">
    开榜之后先用<strong>滚轮</strong>连拨过这么多行再开始逐屏检测 bot——那一段必定还是真人。
    <strong>留空 = {{ blind_scroll_rows_default }} 行</strong>。
    实测约 <strong>{{ rows_per_notch }} 行/格</strong>、每格 16ms，所以 <span id="blind-rows-seconds"></span>。
  </p>
  <p class="muted">
    填大填小由你定。参考：已记录的「实测到达 bot 区用了多少行」最小值是
    {{ blind_scroll_rows_max }} 行——那只是<strong>观测值</strong>，不是上限。
  </p>
  <div class="filter-bar" style="margin-top:10px">
    <input id="blind-scroll-rows" type="number" min="0" step="1"
           placeholder="留空 = {{ blind_scroll_rows_default }}" aria-label="军力榜盲滚行数">
    <button class="btn primary small" type="button" id="btn-save-blind-rows">保存盲滚行数</button>
  </div>
  <p id="blind-rows-slack" class="overview-note"></p>
</section>
```

配套 JS：输入变化时算 `rows / ROWS_PER_NOTCH * WHEEL_GAP_S + GLIDE_SETTLE_S`，
显示成「约 N 秒」。**不做越界判断、不标红。**
读回时用 `?? ''`（0 是合法取值，`||` 会把它显示成空框）。

- [ ] **Step 4: 跑，确认通过**

Run: `python -m pytest tests/integration/api/ -q`

- [ ] **Step 5: 提交**

```bash
git add src/evo_helper/web/ tests/integration/api/
git commit -m "feat(军力榜盲滚): 设置页加盲滚行数，附行↔秒换算显示"
```

---

## Task 8: 调度器接线与命令行

**Files:**
- Modify: `src/evo_helper/domain/missions.py`（`ranking_command` 约 141–170 行）
- Modify: `src/evo_helper/application/mission_scheduler.py`（`_blind_scrolls` 约 3582–3670 行；`validate_blind_scrolls` 约 1051 行；调用点 2433 / 3564 行）
- Modify: `src/evo_helper/tools/ranking_scan.py`（`--blind-rows` 参数）
- Test: `tests/integration/application/test_ranking_blind_scrolls.py`（改）

- [ ] **Step 1: 写失败的测试**——手填优先、留空走默认、留空且样本够时走自标定；命令行里出现 `--blind-rows`；负数被 `MissionParamError` 拒。

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 实现**——`ranking_command(..., blind_rows=...)`；`_blind_rows()` 取值顺序与原 `_blind_scrolls` 一致；`record_knob_override` 只在**取值变化时**写日志（同既有先例）。

- [ ] **Step 4: 跑，确认通过**

Run: `python -m pytest tests/integration/application/ -q`

- [ ] **Step 5: 提交**

```bash
git add src/evo_helper/domain/missions.py src/evo_helper/application/mission_scheduler.py src/evo_helper/tools/ranking_scan.py tests/integration/application/
git commit -m "feat(军力榜盲滚): 调度器与命令行改走 blind_rows"
```

---

## Task 9: 盲滚日志落 `system_log`

**Files:**
- Modify: `src/evo_helper/tools/ranking_scan.py`
- Test: `tests/integration/application/test_ranking_blind_scroll_log.py`

- [ ] **Step 1: 写失败的测试**——跑一趟假采集，断言 `system_log` 里有一条盲滚记录，且 `payload_json` 含 `rows_requested` / `notches_sent` / `spin_seconds` / `glide_seconds` / `rows_per_notch_observed` / `rows_to_bot_area` / `source`。

- [ ] **Step 2: 跑，确认失败**

- [ ] **Step 3: 实现**

⚠️ `rows_per_notch_observed` 是这条日志的要害：`ROWS_PER_NOTCH` 只有 2 个样本，
一旦漂了盲滚距离就静默变化。把每轮实测值记进库，事后才答得出「这个标定还成不成立」。

- [ ] **Step 4: 跑，确认通过**

- [ ] **Step 5: 提交**

```bash
git add src/evo_helper/tools/ranking_scan.py tests/integration/application/
git commit -m "feat(军力榜盲滚): 每轮盲滚落库，含实测每格行数以便回答「标定还成不成立」"
```

---

## Task 10: 全量验证与文档

- [ ] **Step 1: 四条基线**

```bash
python -m pytest tests -q && python -m ruff check src tests && python -m ruff format --check src tests && python -m mypy src
```

⚠️ **mypy 的判据是「不新增」，不是「全绿」**：`main` 上本来就有 3 条
（`domain/intel_query.py:88`、`application/mission_scheduler.py:2973` ×2）。
⚠️ **必须用裸 `python -m mypy src`**（mypy 1.14.1，在 `pyproject` 声明的 `>=1.11,<2` 内）。
`.venv` 里那个是 2.3.0、超出声明范围，会把上面 3 条判成通过——结果不作数。

- [ ] **Step 2: 改写 `docs/军力榜翻页-滚轮实测.md`**——主结论「不能」被推翻，换成「事件形状决定成败」，并把钩子实测证据、`pyautogui.scroll` 不乘 120、单事件封顶三条写进去。**保留原文的量法与三个坑**，那部分仍然成立。

- [ ] **Step 3: 写 `.changes/` 片段**（照 `template.md`）

- [ ] **Step 4: 提交**

---

## Task 11: 实机复测闸门（上生产之前必须做）

⚠️ **`ROWS_PER_NOTCH = 1.08` 只有 2 个样本、1 台机器、1 次会话。它一漂，「盲滚 700 行」实际走的就不是 700 行，而这个偏差是静默的。**

- [ ] **Step 1: 在实机上跑 5 组**（40 / 80 / 160 / 320 / 480 格），确认线性成立且 1.08 落在样本区间内
- [ ] **Step 2: 对不上就改 `ROWS_PER_NOTCH`，不改判据**
- [ ] **Step 3: 迁移由生产在重启时自升；本次不对任何生产库执行 `alembic upgrade`**
