"""在派遣面板里选一个游戏内预设。

**为什么必须显式选预设**：派遣面板会保留上一次的选择。实机上打开时那里躺着
「轻型战斗机 1000」——直接点绿✓再出发，就把一千架轻型战斗机送去打海盗了。
所以攻击链路必须「选预设 → 回读校验」，两步都不能省。

预设条是**连续横向滚动**的，一屏只看得见约两个预设，而且**打开时的滚动位置不固定**：
实机上 AAA 在最左端，面板却是从「探路 / BBB」那一段打开的。所以流程是
「先拖到左端夹住 → 再按名字找」，不能假设第一屏就有想要的那个。

⚠️ **预设条最右端是「+ 保存当前舰队」**，点到它会覆盖用户的预设——这是整条链路上
唯一会改坏用户配置的控件。所以这里**只往左拖**（`PRESET_DRAG_TO_X → FROM_X`，
内容右移、露出左侧），从不往右拖：往左拖永远离那个按钮更远。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from evo_helper.game.pirate_ui import (
    PRESET_DRAG_FROM_X,
    PRESET_DRAG_TO_X,
    PRESET_DRAG_Y,
    PRESET_MAX_DRAGS,
    PRESET_NAME_ROW_Y,
    PRESET_TOGGLE,
)

#: 预设名那一行的 ROI 与 OCR 配方（917 空间，实机量于 2026-08-09）。
#: 右界收到 1000：再往右是第二个预设的数量列，读进来只是噪声。
#: 3× + `chi_sim+eng` + psm 6 实测能同时读出 `AAA` 与 `探路`；
#: 只跑 `eng` 会把中文预设名读成 `PRIS` 之类，于是中文名的预设永远选不中。
PRESET_NAME_ROI = (730, 684, 1000, 704)
PRESET_NAME_UPSCALE = 3

#: 展开预设条后等它铺开。
PRESET_OPEN_WAIT_S = 1.6

#: 一次拖动之后等惯性停下。
PRESET_DRAG_WAIT_S = 1.2


class PresetDriver(Protocol):
    def click(self, x: int, y: int, *, label: str = ...) -> None: ...

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str = ...) -> None: ...

    def wait(self, seconds: float) -> None: ...


class PresetNotFound(RuntimeError):
    """拖到左端也没在预设条上找到这个名字。

    **不许退而求其次点一个别的**：预设决定送出去多少舰队，选错的代价是真实的舰队。
    """


@dataclass
class PresetPicker:
    """按名字选预设。`read_names` 交出这一屏预设名的 `(中心 x, 文字)`。"""

    driver: PresetDriver
    read_names: Callable[[], Sequence[tuple[int, str]]]

    def expand(self) -> None:
        self.driver.click(*PRESET_TOGGLE, label="预设条")
        self.driver.wait(PRESET_OPEN_WAIT_S)

    def scroll_to_left_end(self, *, max_drags: int = PRESET_MAX_DRAGS) -> Sequence[tuple[int, str]]:
        """一直往左拖到夹住，返回夹住之后这一屏的预设名。

        判据是「这一屏读到的名字不再变化」，不是拖固定次数：拖多少次能到左端
        取决于打开时停在哪。实测从「探路 / BBB」那一段出发，两次就夹住了。
        """
        seen = list(self.read_names())
        for _attempt in range(max_drags):
            self.driver.drag(
                PRESET_DRAG_TO_X,
                PRESET_DRAG_Y,
                PRESET_DRAG_FROM_X,
                PRESET_DRAG_Y,
                label="预设条左移",
            )
            self.driver.wait(PRESET_DRAG_WAIT_S)
            current = list(self.read_names())
            if _names_of(current) == _names_of(seen):
                return current
            seen = current
        return seen

    def pick(self, name: str) -> int:
        """展开、拖到左端、点中名叫 `name` 的那个预设，返回它的中心 x。

        找不到就抛 `PresetNotFound`——由调用方决定放弃这一发，而不是凑合点一个。
        """
        self.expand()
        entries = self.scroll_to_left_end()
        hits = [x for x, text in entries if name in text]
        if not hits:
            raise PresetNotFound(
                f"预设条上找不到 {name!r}；这一屏读到的是 {[text for _x, text in entries]}"
            )
        # 同名多个只可能是 OCR 把一个名字拆成了两块词，取最左那个即可。
        target = min(hits)
        self.driver.click(target, PRESET_NAME_ROW_Y, label=f"预设 {name}")
        self.driver.wait(PRESET_DRAG_WAIT_S)
        return target


def name_words(image: Any, ocr: Any) -> list[tuple[int, str]]:
    """从一张整窗截图里读出预设名那一行的 `(中心 x, 文字)`。

    用词框而不是整行文本：要拿 x 去点。
    """
    crop = image.crop(PRESET_NAME_ROI).convert("L")
    grey = crop.resize(
        (crop.width * PRESET_NAME_UPSCALE, crop.height * PRESET_NAME_UPSCALE),
        _lanczos(image),
    )
    data = ocr.image_to_data(
        grey, lang="chi_sim+eng", config="--psm 6", output_type=ocr.Output.DICT
    )
    words: list[tuple[int, str]] = []
    for index, word in enumerate(data["text"]):
        text = word.strip()
        if not text:
            continue
        left = PRESET_NAME_ROI[0] + data["left"][index] // PRESET_NAME_UPSCALE
        width = data["width"][index] // PRESET_NAME_UPSCALE
        words.append((left + width // 2, text))
    return words


def _lanczos(image: Any) -> Any:
    from PIL import Image

    del image
    return Image.Resampling.LANCZOS


def _names_of(entries: Sequence[tuple[int, str]]) -> list[str]:
    return [text for _x, text in entries]


__all__ = [
    "PRESET_NAME_ROI",
    "PresetNotFound",
    "PresetPicker",
    "name_words",
]
