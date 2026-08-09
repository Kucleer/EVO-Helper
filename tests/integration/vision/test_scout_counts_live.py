"""用真实侦察报告截图守住一次实机事故：全 0 的海盗被读成有舰队，真的挨了一发。

事故（2026-08-09）：`named_counts` 当时用 `number_column()` 现场量数字列，而
「四项全 0」的清单里每格只有一个窄窄的 `0`，够宽的墨迹段只剩面板左边那层水印
（`-17003` / `COMMAND OFFICERS`）——量出来的「数字列」是 (731, 808)，读到的
「数量」是水印里的数字。判定因此把 2:137:2 当成有舰队，`pirate_loop` 打了出去。
同一封报告两次读成 `{'噬能截击者': 8}` 与 `{'深空吞噬者': 2}`，前后不一致就是指纹。

这两条用例是**方向性**的，不追求逐格精确：

- 全 0 那封 → **一定不能**判成「打」。
- 有舰队那封 → 一定要判成「打」。

截图在 `var/` 下，不进 Git，所以缺图时整个文件跳过。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evo_helper.game.pirate_ui import PIRATE_TRIGGER_SHIPS, triggers_attack

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

#: 拖到底那一屏。`scout-zero-ships` 是四项全 0 的那封（09/08 02:54:43），
#: `scout1-ships` 是 2:137:4 那封（深空吞噬者 2 / 噬能截击者 4 / 钛能守卫者 4）。
ZERO = Path("var/logs/scout-zero-ships.png")
FLEET = Path("var/logs/scout1-ships.png")
TESSERACT = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

pytestmark = pytest.mark.skipif(
    not (ZERO.is_file() and FLEET.is_file() and Path(TESSERACT).is_file()),
    reason="侦察报告截图或 Tesseract 不在",
)


def _counts(path: Path) -> dict[str, int]:
    from evo_helper.vision.optional.report_screens import ImageReportScreens
    from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport
    from evo_helper.vision.scout_reports import (
        SCOUT_COUNT_BAND,
        SCOUT_SHIP_BAND,
        SCOUT_SHIP_BOTTOM,
        SCOUT_SHIP_TOP,
    )

    image = crop_to_viewport(Image.open(path))
    screens = ImageReportScreens(
        image, layout_for_viewport(image.width, image.height), tesseract_cmd=TESSERACT
    )
    return screens.named_counts(
        PIRATE_TRIGGER_SHIPS,
        SCOUT_SHIP_BAND,
        SCOUT_SHIP_TOP,
        SCOUT_SHIP_BOTTOM,
        count_band=SCOUT_COUNT_BAND,
    )


def test_an_empty_pirate_is_never_read_as_having_a_fleet() -> None:
    counts = _counts(ZERO)

    assert not triggers_attack(counts), f"全 0 的报告读出了 {counts}"
    assert all(value == 0 for value in counts.values()), counts


def test_a_real_fleet_is_still_read() -> None:
    """修法不能把判定修成「永远不打」——那样它就白装了。"""
    counts = _counts(FLEET)

    assert triggers_attack(counts), counts
    assert counts.get("深空吞噬者") == 2, counts
