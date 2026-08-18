"""真实战报面板上的攻/守坐标读得对不对。

## 为什么非要真图

`9` 被读成 `3` 这件事只发生在**那个字号、那个字体、那一档放大滤波**上：
`[9` 在 7× LANCZOS 下糊成一个 `3`（`[2:91:9]` → `[2:91:39]` 是同一族的老坑，
见 `vision.scan_reading.COORD_RECIPES`）。合成夹具复现不了它——能复现的话，
当初也就不需要四套配方了。

## 图从哪来，为什么不在 Git 里

`battle_report_screenshots` 里存着每份战报的面板（视口的 (700,105)-(1220,800)，
520×695）。**本仓是公开仓库**，战报面板上写着账号名、出发星与目标坐标、逐格的
资源数量，所以图一张都不许进来（`.gitignore` 里那一段记着 2026-08-18 那次
34 张实拍进仓的事故）。判据照 `tests/integration/vision/*_live.py` 的老规矩办：
**图放 `var/`、缺图就 skip**。

导出办法（只读连生产库；`image_bytes` 是 WEBP）::

    SELECT s.image_bytes, r.defender_target_*, i.origin_*
      FROM battle_report_screenshots s
      JOIN battle_reports r ON r.id = s.report_id
      ...
    -- 存成 var/logs/report-panels/panel_att-<G-S-P>_def-<G-S-P>_<任意后缀>.webp

⚠️ **文件名里那两个坐标是真值，不是「上次读出来的」**：攻方取的是那一发派遣
记录上的出发星球（`attack_intents.origin_*`），守方取的是战报的目标。攻方要是
照抄战报读数，这个用例就会把 `3:250:8` 当成期望值——那正是要抓的那个错。

## ⚠️ 面板是 WEBP q90（有损）

存档那一步本来就不是为喂 OCR 准备的（`ImageReportScreens.report_panel_image`
上写着「这一块不喂 OCR，是给人看的」）。所以这里读的像素与实机当时读的**不完全
是同一份**：这个用例证明的是「投票在这批真实像素上读对了、逐套配方里确实有一套
读错」，不是逐比特复现实机那一刻。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from evo_helper.vision.report_layout import LIVE_LAYOUT, layout_for_viewport

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

PANELS = Path("var/logs/report-panels")
TESSERACT = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
NAME_RE = re.compile(r"panel_att-(\d+-\d+-\d+)_def-(\d+-\d+-\d+)")

#: 标定视口。面板是它的一块裁剪，贴回原位之后逐像素等价（同
#: `vision.optional.panel_resources` 那条「平移 ROI 而不是重量一套」的理由）。
VIEWPORT = (1920, 879)


def _panels() -> list[Path]:
    return sorted(path for path in PANELS.glob("*.webp") if NAME_RE.match(path.name))


pytestmark = pytest.mark.skipif(
    not (PANELS.is_dir() and _panels() and Path(TESSERACT).is_file()),
    reason="var/logs/report-panels 里没有面板图，或者本机没装 Tesseract",
)


def _screens(path: Path):  # type: ignore[no-untyped-def]
    """把存档面板贴回视口原位，再交给实机那条读数路径。

    贴回去而不是「另量一套面板坐标系的 ROI」：版面常量只有一套，另量一份等于把
    同一件事记两遍，游戏改版时它们会各改各的，而对不上的那一刻没有任何症状。
    """
    from evo_helper.vision.optional.report_screens import ImageReportScreens

    panel = Image.open(path).convert("RGB")
    expected = (
        LIVE_LAYOUT.report_panel.right - LIVE_LAYOUT.report_panel.left,
        LIVE_LAYOUT.report_panel.bottom - LIVE_LAYOUT.report_panel.top,
    )
    assert (panel.width, panel.height) == expected, f"{path.name} 不是一块战报面板"
    canvas = Image.new("RGB", VIEWPORT)
    canvas.paste(panel, (LIVE_LAYOUT.report_panel.left, LIVE_LAYOUT.report_panel.top))
    return ImageReportScreens(canvas, layout_for_viewport(*VIEWPORT), tesseract_cmd=TESSERACT)


@pytest.mark.parametrize("path", _panels(), ids=lambda path: path.stem)
def test_both_coordinates_read_as_the_dispatch_recorded_them(path: Path) -> None:
    """⚠️ 攻方那一半就是实机故障本身：`9:250:8` 的 7 份战报**全部**读成了 `3:250:8`。

    多配方投票之前，「第一套读出合法三元组就采信」会把 7l 那一套读出的
    `3:250:8` / `39:250:8` 直接采信，另外三套一致的 `9:250:8` 连看都不看。
    """
    match = NAME_RE.match(path.name)
    assert match is not None
    attacker = match.group(1).replace("-", ":")
    defender = match.group(2).replace("-", ":")

    block = _screens(path).versus_block()

    assert attacker in block, f"攻方出发点读错了：期望 {attacker}，读到 {block!r}"
    assert defender in block, f"守方目标读错了：期望 {defender}，读到 {block!r}"
