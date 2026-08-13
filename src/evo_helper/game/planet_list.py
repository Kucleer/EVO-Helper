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


class SwitchResult(Enum):
    """一次切换的结局。

    `NOT_FOUND` 与 `UNCONFIRMED` 分开而不是合并成一个 False：前者是**一次点击
    都没发生**（列表里翻不到这颗星球，多半是配错了坐标），后者是点过了但回读
    没认出来（可能切成了、可能没有）。两句话对用户的意思完全不同，而处置一样：
    本轮不派。
    """

    SWITCHED = "switched"
    NOT_FOUND = "not_found"
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
    #: 每一屏读到的行，按顺序记下来，找不到时原样说出去（照 `PresetNotFound` 的做法）。
    screens: list[list[str]] = field(default_factory=list)

    def switch_to(self, target: Coordinate) -> SwitchResult:
        """切到 `target`，返回结局。**任何一步认不出都不点**。"""
        self.screens = []
        self.driver.click(*NAV_PLANET, label="行星列表")
        self.driver.wait(PLANET_LIST_OPEN_WAIT_S)
        row = self._locate(target)
        if row is None:
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
        return self._confirm(target)

    # -- 找那一行 -----------------------------------------------------------

    def _locate(self, target: Coordinate) -> PlanetRow | None:
        """一屏一屏找，找到就把**当屏刚回读过**的那一行交出去。

        每一轮都重读两次：第一次找，第二次是点击前的复核。复核对不上就当这一屏
        没有，继续拖——一次 OCR 抖动不该换来一次点击。
        """
        rows = rows_from_words(self.read_rows())
        previous: Sequence[PlanetRow] | None = None
        for attempt in range(PLANET_LIST_MAX_DRAGS + 1):
            self.screens.append([row.text for row in rows])
            hit = find_row(rows, target)
            if hit is not None:
                again = find_row(rows_from_words(self.read_rows()), target)
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
            rows = rows_from_words(self.read_rows())
        return None

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
        面板本身是同一块（标定图 `calib-舰队面板-client.png`），
        绿✓ 在 `DISPATCH_CONFIRM`，这里一步都不靠近它，读完就点 ✕。
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
    data = ocr.image_to_data(
        grey,
        lang="eng",
        config=f"--psm 6 -c tessedit_char_whitelist={whitelist}",
        output_type=ocr.Output.DICT,
    )
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
