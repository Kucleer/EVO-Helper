"""行星列表浮层：在游戏里把「当前星球」切到指定坐标。

薄薄一层：**拍一屏 → 交给 `domain.planet_switch` → 按它给的坐标点一下**。
判据一条都不在这里，坐标常量一条都不在这里（都在 `game.pirate_ui`）。

    星球地表 / 恒星系视图
      → 点底部导航「行星」`pirate_ui.NAV_PLANET` → 行星列表浮层
      → 拖 + 每屏认坐标，找到目标那一行
      → 点该行的「前往此处」
      → 开派遣面板回读「起点」，确认真的换了

## 三条不许妥协的

1. **先认坐标再点。** 目标不在这一屏读出来的坐标里就什么都不点；一路拖到底还是
   没有，仍然什么都不点，返回 `NOT_FOUND` 让调用方本轮别派。绝不按行号盲点——
   那一排里转移/投送/保护/扩张点错任何一个都是真实操作。
2. **点之前再回读一次那一行**（与 `game.action_guard` 的「点击前重新观察」同形）。
   两次读的必须是同一屏、同一个 y；对不上就当这一屏没找到，接着拖。
3. **只有「前往此处」那一个 x 进代码**（`pirate_ui.PLANET_GOTO_COLUMN_X`），
   其余七个图标的坐标本仓根本不存在。

## 一行都没读出来 ≠ 列表里没有这颗星球

前者多半是**有别的浮层盖着导航栏**，那时点「行星」那一下压根没打开任何东西；
后者才是「配错了出发星球」。两种的善后完全相反，所以只有前者才去关浮层重读一遍
（`PLANET_LIST_OVERLAY_RETRIES`，用的是全仓共用的 `game.overlay`）。

而且**结局也必须是两个**：读不出来是 `SwitchResult.UNREADABLE`，翻通了没有才是
`NOT_FOUND`。把前者说成后者，日志就会指着用户的配置说一句假话，而调用方还会照
「不会自己好」把这一轮记成失败——整段账在 `SwitchResult` 上。

## 拖动用慢拖，不用一步式 drag

`tools.pirate_loop.slow_drag` 的注释里写着：一步到位的 `dragTo` 会被游戏面板
**当成点击**，同样的起止点有时滚有时不滚。而这里按下的那一点就在星球名那一行，
被当成点击时点的是那一行的空白处——运气好没事，运气不好就是下一次版面微调之后
按在了图标上。所以驱动面上要的是 `drag_vertical`，实机接的是慢拖。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from evo_helper.domain.models import Coordinate
from evo_helper.domain.planet_switch import (
    PlanetRow,
    find_row,
    list_exhausted,
    origin_confirmed,
    rows_from_words,
)
from evo_helper.game.overlay import OVERLAY_CLOSE_ATTEMPTS, dismiss_overlays
from evo_helper.game.pirate_ui import (
    FLEET_PANEL_OPEN_WAIT_S,
    NAV_FLEET,
    NAV_PLANET,
    PLANET_GOTO_COLUMN_X,
    PLANET_ICON_ROW_OFFSET_Y,
    PLANET_LIST_CLOSE,
    PLANET_LIST_COORD_ROI,
    PLANET_LIST_DRAG_TO_Y,
    PLANET_LIST_DRAG_WAIT_S,
    PLANET_LIST_DRAG_X,
    PLANET_LIST_MAX_DRAGS,
    PLANET_LIST_MIN_DRAG_PX,
    PLANET_LIST_OPEN_WAIT_S,
    PLANET_SWITCH_WAIT_S,
)

#: 刚展开/拖动完的列表偶尔会在一帧内 OCR 成空；列表本身不可能没有任何行。
#: 这与预设条的空读同形：把它直接当成「目标不存在」会让本轮在切换出发星球前
#: 安全退出，连预设选择都走不到。有限重读能等过动画，又不会卡死在真故障画面上。
PLANET_LIST_READ_ATTEMPTS = 3
PLANET_LIST_REREAD_WAIT_S = 0.6
#: 一张行星列表的 OCR 绝不能无限占住攻击轮。超时一律当作本帧没读到；
#: ``PlanetSwitcher`` 会按既有的确认策略重读，仍为空则安全地不点击。
PLANET_LIST_OCR_TIMEOUT_S = 8.0

#: 「一行都没读出来」时，关掉浮层再重开列表读一遍——**只此一遍**。
#:
#: ⚠️ 实机事故（2026-08-17 11:20–11:40）：游戏停在「太空舱」面板上（材料/星云/
#: 加速器/资源/舰长/行星工具 那一屏），它把整条底部导航栏连同行星列表一起盖住了。
#: 于是每一轮都是同一段：点 `NAV_PLANET` 那一下落在浮层上、什么也没打开 → 逐屏
#: 读到 `[[]]` → 判 `NOT_FOUND` → 「这一轮一发都不派」。**连续多轮，一发没派。**
#: 而关浮层的机制早就有（`game.overlay`），只是从没接到这条链路上——攻击链路里
#: 没有任何一步会去关那个面板。
#:
#: 上限是 1：关完重读还是空，就是真的读不出（OCR 配置、视口漂了、界面改版），
#: 那时按 `SwitchResult.UNREADABLE` 安全退出——**不是 `NOT_FOUND`**，读不出来
#: 说不出列表里有什么。做成循环重试只会把整轮卡死在一个面板上。
PLANET_LIST_OVERLAY_RETRIES = 1


class SwitchResult(Enum):
    """一次切换的结局。

    `NOT_FOUND` 与 `UNCONFIRMED` 分开而不是合并成一个 False：前者是**一次点击
    都没发生**（列表里翻不到这颗星球，多半是配错了坐标），后者是点过了但回读
    没认出来（可能切成了、可能没有）。两句话对用户的意思完全不同，而处置一样：
    本轮不派。

    ⚠️ **`UNREADABLE` 与 `NOT_FOUND` 必须分开，混着用就是在污蔑用户的配置。**

    实机 2026-08-18 10:04:07 与 10:05:10 各一次，日志原文：

        切不到出发星球 4:277:15（not_found）；这一轮一发都不派
        这颗星球不在你的行星列表里；请核对任务配的出发星球 4:277:15

    **这句话是假的**——4:277:15 就是用户的主星。真实原因是那两轮列表一行都没
    读出来（画面上压着军力榜面板）。而调用方照 `NOT_FOUND` 把
    `Outcome.busy_is_permanent` 置了真，于是退出码 1、计入连续失败、走向自动停用；
    连着两轮 exit=1。

    判据很硬，**看的是逐屏读到的行，不是「找没找到目标」**：

        逐屏里只要有过内容 → NOT_FOUND    列表读通了、里面确实没有这颗星球
        全部为空           → UNREADABLE   压根没读出列表，说不出里面有什么

    两者的善后相反：前者该指着配置说话、该计失败（不会自己好）；后者只能说
    「这一轮读不出来」，**不停用、不计失败**（调用方按 `EXIT_ENVIRONMENT_BUSY`
    收场，见 `tools.pirate_loop.exit_code_for`）。
    """

    SWITCHED = "switched"
    NOT_FOUND = "not_found"
    UNREADABLE = "unreadable"
    UNCONFIRMED = "unconfirmed"
    DRY_RUN = "dry_run"


class PlanetListDriver(Protocol):
    def click(self, x: int, y: int, *, label: str = ...) -> None: ...

    def drag_vertical(self, x: int, from_y: int, to_y: int, *, label: str = ...) -> None: ...

    def wait(self, seconds: float) -> None: ...


@dataclass
class PlanetSwitcher:
    """把当前星球切到指定坐标。

    `read_rows` 交出**当前这一屏**坐标列的 `(中心 y, 文字)` 词框；
    `read_origin` 交出派遣面板「起点」那一行的读数。两者都由调用方注入——
    这一层不认识 OCR，测试里也就不需要假图片。

    `dry_run` 走完「开浮层 → 拖 → 认坐标」，然后只打印「打算点哪里、因为读到了
    什么」，**不点「前往此处」、也不去回读**。开浮层和拖动本身留着不是偷懒：
    要给人看的正是「它认到的是不是那一行」，而那个答案只能从真实画面上来。
    这两个动作都只翻自己的星球清单，不动任何东西。
    """

    driver: PlanetListDriver
    read_rows: Callable[[], Sequence[tuple[int, str]]]
    read_origin: Callable[[], str]
    say: Callable[[str], None] = print
    dry_run: bool = False
    #: 走到「关浮层重读」那一支时留下的现场：`(一句话, 结构化 payload)`。
    #:
    #: 默认空操作，实机由 `tools.pirate_loop` 接到 `system_log`（还捎一张缩略图）。
    #: 不在这一层直接写库：`game/` 整层都不认识 `infrastructure/`，而且真写进去了
    #: 单元测试就得有库。
    record_evidence: Callable[[str, dict[str, Any]], None] = lambda _message, _payload: None
    #: 「画面上那个 ✕ 现在在不在」。**关浮层之前问它**，答否就一下都不点。
    #:
    #: 默认 `False` 是**故意的保守值**：没接这个回读的调用方（轻量工具、单元测试
    #: 桩）走的是「从不关浮层」，而不是「照旧盲点」。判据本身在 `game.overlay`，
    #: 实机由 `tools.pirate_loop` 接到 `LiveDriver.capture()`。
    see_close_button: Callable[[], bool] = lambda: False
    #: 每一屏读到的行，按顺序记下来，找不到时原样说出去（照 `PresetNotFound` 的做法）。
    screens: list[list[str]] = field(default_factory=list)

    def switch_to(self, target: Coordinate) -> SwitchResult:
        """切到 `target`，返回结局。**任何一步认不出都不点**。"""
        self.screens = []
        row = self._open_and_locate(target)
        if row is None and self._read_nothing_at_all():
            row = self._retry_behind_overlays(target)
        if row is None:
            # ⚠️ 判据看的是**整趟逐屏读到了什么**（含重读那一遍），不是「找没找到」。
            # 全空 = 说不出列表里有什么，那时指着用户的配置说话就是在造假；
            # 读到过内容 = 列表确实翻通了，这颗星球真的不在里面。见 `SwitchResult`。
            if self._read_nothing_at_all():
                self.say(f"  行星列表一行都没读出来；说不出里面有没有 {target}；什么都不点")
                self._close()
                return SwitchResult.UNREADABLE
            self.say(f"  行星列表上找不到 {target}；逐屏读到的是 {self.screens}；什么都不点")
            self._close()
            return SwitchResult.NOT_FOUND
        point = (PLANET_GOTO_COLUMN_X, row.name_row_y + PLANET_ICON_ROW_OFFSET_Y)
        self.say(f"  打算点 {point}，因为这一屏在 y={row.name_row_y} 读到 {row.text}")
        if self.dry_run:
            self._close()
            return SwitchResult.DRY_RUN
        self.driver.click(*point, label=f"前往 {target}")
        self.driver.wait(PLANET_SWITCH_WAIT_S)
        # ⚠️ 这里**故意不 `_close()`**，另外两个出口都关。点完「前往此处」浮层会
        # 自己关掉（用户实机确认 2026-08-13），画面直接落到新星球上，已经没有浮层
        # 可关了。这一条是隐形依赖，所以写在这儿：浮层横跨 x≈740-1230、y≈71-890，
        # 而 `_confirm` 下一步要点的 `NAV_FLEET`(920, 862) 正在这个范围里——浮层
        # 要是没关，那一下就点在浮层上，回读读不出、整轮白等。
        #
        # 反过来，顺手在这里补一个 `_close()` 同样是错的：那时点的 (750, 71) 落在
        # 新星球的画面上，那个位置上有什么本仓没有标定过。
        return self._confirm(target)

    # -- 开列表、找那一行 ---------------------------------------------------

    def _open_and_locate(self, target: Coordinate) -> PlanetRow | None:
        """点开行星列表浮层，然后一屏一屏找。找不到返回 None。"""
        self.driver.click(*NAV_PLANET, label="行星列表")
        self.driver.wait(PLANET_LIST_OPEN_WAIT_S)
        return self._locate(target)

    def _read_nothing_at_all(self) -> bool:
        """逐屏一行都没认出来吗？——这才是「疑似有浮层盖住」的证据。

        ⚠️ **这道界限是本支的全部要害。** 读到了内容却没有目标那一行，是**另一回事**：
        多半是任务配错了出发星球（`SwitchResult.NOT_FOUND` 的注释里写着这一条，
        `Outcome.busy_is_permanent` 也是照它分流的）。把那种情况也当成「被盖住了」，
        代价是每一次配错坐标都要先朝 (750, 71) 盲点四下——而**星球地表上那个位置
        本仓没有标定过**（见 `switch_to` 里那段告警）。所以：只在一行都没有时才关。

        注意「一行都没有」用的是 `screens`，也就是 `rows_from_words` **认出来的
        坐标行**，不是 OCR 的原始词框。行星大小 `155/223`、图标漏出来的零星 `5`
        这些噪声算不得「读到了列表」。
        """
        return bool(self.screens) and all(not screen for screen in self.screens)

    def _retry_behind_overlays(self, target: Coordinate) -> PlanetRow | None:
        """关掉盖在上面的浮层，重开列表再读一遍。见 `PLANET_LIST_OVERLAY_RETRIES`。

        动作顺序是有讲究的：**先关再开**。这一刻列表多半根本没开出来（点 `NAV_PLANET`
        那一下落在浮层上），所以那几下 ✕ 关的是压在导航栏上的那个面板；关完必须
        重新点一次「行星」，否则读的还是同一张什么都没有的画面。

        ⚠️ **认不出那个 ✕ 就一下都不点**（`see_close_button`，2026-08-18 用户指出）。
        实机撞到过：那一刻画面上是**军力排行榜面板**，而原先的盲点把 4 下全落进了
        榜单里。认不出时这一支等于什么都没做，本轮照常安全退出——代价只是这一轮
        不派，远小于在榜单/列表上误触一次真实操作。
        """
        before = [list(screen) for screen in self.screens]
        self.say("  行星列表一行都没读出来；疑似有浮层盖住了导航栏，先关掉浮层再读一遍")
        clicked = 0
        recognised = False
        row: PlanetRow | None = None
        for _attempt in range(PLANET_LIST_OVERLAY_RETRIES):
            dismissed = dismiss_overlays(
                self.driver,
                see_close_button=self.see_close_button,
                attempts=OVERLAY_CLOSE_ATTEMPTS,
            )
            clicked += dismissed.clicked
            recognised = recognised or dismissed.recognised
            if not dismissed.recognised:
                # 那个位置上不是 ✕（实机见过：军力榜面板、恒星系视图的导航输入框）。
                # 不点，也不重开列表——重开只会再读一张同样的画面。
                self.say("  关闭键那个位置上认不出 ✕；一下都不点，本轮就此退出")
                break
            row = self._open_and_locate(target)
            if row is not None:
                break
        after = [list(screen) for screen in self.screens[len(before) :]]
        if not recognised:
            verdict = "认不出关闭键 ✕，没点也没重读"
        elif row is not None:
            verdict = f"重读认到了 {target}"
        elif any(after):
            verdict = "重读读到了内容但没有这颗星球"
        else:
            verdict = "重读还是一行都没有"
        self.say(f"  关浮层点了 {clicked} 下；{verdict}（重读逐屏 {after}）")
        self.record_evidence(
            f"行星列表读空（逐屏 {before}）；疑似有浮层盖住导航栏，"
            f"已点 {clicked} 下关闭键并重开列表；{verdict}（重读逐屏 {after}）",
            {
                "target": str(target),
                "screens_before": before,
                "screens_after": after,
                "close_clicks": clicked,
                "close_button_recognised": recognised,
                "recovered": row is not None,
            },
        )
        return row

    # -- 找那一行 -----------------------------------------------------------

    def _locate(self, target: Coordinate) -> PlanetRow | None:
        """一屏一屏找，找到就把**当屏刚回读过**的那一行交出去。

        每一轮都重读两次：第一次找，第二次是点击前的复核。复核对不上就当这一屏
        没有，继续拖——一次 OCR 抖动不该换来一次点击。
        """
        rows = rows_from_words(self._read_rows_confirming())
        previous: Sequence[PlanetRow] | None = None
        for attempt in range(PLANET_LIST_MAX_DRAGS + 1):
            self.screens.append([row.text for row in rows])
            hit = find_row(rows, target)
            if hit is not None:
                again = find_row(rows_from_words(self._read_rows_confirming()), target)
                if again is not None and again.name_row_y == hit.name_row_y:
                    return again
                self.say("  点击前复核对不上（这一行动了或者没读出来）；不点，接着拖")
            if previous is not None and list_exhausted(previous, rows):
                return None  # 拖到底了，下面没有更多星球。
            if attempt == PLANET_LIST_MAX_DRAGS:
                return None  # 拖满上限；宁可这一轮不派，也不无限拖下去。
            previous = rows
            if not self._drag_once(rows):
                return None
            rows = rows_from_words(self._read_rows_confirming())
        return None

    def _read_rows_confirming(self) -> Sequence[tuple[int, str]]:
        """空行不是列表为空的证据，重读几帧再交给定位逻辑。"""
        for _attempt in range(PLANET_LIST_READ_ATTEMPTS):
            words = self.read_rows()
            if words:
                return words
            self.driver.wait(PLANET_LIST_REREAD_WAIT_S)
        return ()

    def _drag_once(self, rows: Sequence[PlanetRow]) -> bool:
        """按住**这一屏最下面那一行的名字高度**往上拖一段；拖不动就返回 False。

        按下点跟着当前这一屏走，不写死：见 `domain.planet_switch.PlanetRow`。
        """
        if not rows:
            return False
        anchor = rows[-1].name_row_y
        if anchor - PLANET_LIST_DRAG_TO_Y < PLANET_LIST_MIN_DRAG_PX:
            return False
        self.driver.drag_vertical(
            PLANET_LIST_DRAG_X, anchor, PLANET_LIST_DRAG_TO_Y, label="行星列表上移"
        )
        self.driver.wait(PLANET_LIST_DRAG_WAIT_S)
        return True

    # -- 回读 ---------------------------------------------------------------

    def _confirm(self, target: Coordinate) -> SwitchResult:
        """开派遣面板读「起点」，确认当前星球真的是 `target`；读不出算没切成。

        开的是**舰队**那个入口（`NAV_FLEET`）而不是从某个目标点「攻击」：
        这一步只读不派，不该为了读一行字先站到一个可攻击目标上去。
        绿✓ 在 `DISPATCH_CONFIRM`，这里一步都不靠近它，读完就点 ✕。

        ⚠️ `FLEET_ORIGIN_ROI` 是在标定图 `calib-舰队面板-client.png` 上量的，
        而**那张图就是底部导航「舰队」开出来的这一块**（用户实机确认 2026-08-13）。
        这句得写下来：ROI 是像素坐标，量在哪块面板上就只在哪块面板上成立；
        要是量的是另一条路径开出来的面板，单元测试与离线实拍会全绿，
        实机上却永远读不出「起点」——于是每一轮都判「没切成」、一发都不派。
        """
        self.driver.click(*NAV_FLEET, label="舰队面板")
        self.driver.wait(FLEET_PANEL_OPEN_WAIT_S)
        raw = self.read_origin()
        self._close()
        if origin_confirmed(raw, target):
            self.say(f"  起点回读 {raw!r}，确认当前星球是 {target}")
            return SwitchResult.SWITCHED
        self.say(f"  起点回读 {raw!r}，对不上 {target}；当作没切成，本轮不派")
        return SwitchResult.UNCONFIRMED

    def _close(self) -> None:
        self.driver.click(*PLANET_LIST_CLOSE, label="关闭浮层")
        self.driver.wait(PLANET_LIST_DRAG_WAIT_S)


def coordinate_words(
    image: Any, ocr: Any, *, upscale: int, resample: str, whitelist: str
) -> list[tuple[int, str]]:
    """从一张整窗截图里读出坐标列的 `(中心 y, 文字)`。

    与 `preset_picker.name_words` 同形，只是那边要 x（横着找预设）、这边要 y
    （竖着找星球）。用词框而不是整行文本：**那个 y 就是待会儿要点、要按的地方**。

    白名单里没有方括号（`]` 会被读成 `3`，见 `vision.scan_reading.COORD_RECIPES`），
    所以 `[2:137:18]` 读回来是 `2:137:18`。
    """
    from PIL import Image

    filters = {"lanczos": Image.Resampling.LANCZOS, "nearest": Image.Resampling.NEAREST}
    crop = image.crop(PLANET_LIST_COORD_ROI).convert("L")
    grey = crop.resize((crop.width * upscale, crop.height * upscale), filters[resample])
    try:
        data = ocr.image_to_data(
            grey,
            lang="eng",
            config=f"--psm 6 -c tessedit_char_whitelist={whitelist}",
            output_type=ocr.Output.DICT,
            timeout=PLANET_LIST_OCR_TIMEOUT_S,
        )
    except RuntimeError:
        # pytesseract 在超时后抛 RuntimeError。这里不能把一次识别失手升级为
        # 整个攻击进程卡死；空结果会沿用「重读 / 不盲点」这条安全路径。
        return []
    words: list[tuple[int, str]] = []
    for index, word in enumerate(data["text"]):
        text = word.strip()
        if not text:
            continue
        top = PLANET_LIST_COORD_ROI[1] + data["top"][index] // upscale
        height = data["height"][index] // upscale
        words.append((top + height // 2, text))
    return words


__all__ = ["PlanetListDriver", "PlanetSwitcher", "SwitchResult", "coordinate_words"]
