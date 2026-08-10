"""用真实简报截图守住一次「读不出来但一路无声」的事故。

事故（2026-08-10 实机标定时发现）：`BRIEFING_FLIGHT_ROI` 从落地起就**从来没有
读出过东西**。调用方 `_read` 的默认是 3× 放大、不二值化，而这一行是绿字压在
蓝底上，灰度化之后对比度不够——实测读出来是 `'-'`。

危险的不是读错，是**读错之后什么都不会发生**：`parse_game_duration('-')` 返回
None 而不抛异常，于是 `expected_report_at_utc` 与 `line_free_at_utc` 恒为 NULL，
「派出后松手、到点回来收战报」和航线释放时刻全部空转，日志上一句话都没有。

为什么此前的测试全绿：它们注入的是假 OCR，验的是「拿到时长之后怎么算」，
而不是「能不能从真实像素里拿到时长」。变异验证也全红——但变异改的是同一层。
**唯独没有一条测试碰过真实像素。**

截图在 `var/` 下，不进 Git，所以缺图时整个文件跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evo_helper.domain.report_wait import parse_game_duration
from evo_helper.game.pirate_ui import FLIGHT_RECIPES

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

#: 侦察简报上那一行的 ROI 裁片，真值「14秒」（2:137:4，2026-08-10）。
FLIGHT_CROP = Path("var/logs/briefing-flight-scout.png")

pytestmark = pytest.mark.skipif(not FLIGHT_CROP.exists(), reason=f"缺实拍截图 {FLIGHT_CROP}")


@pytest.fixture(scope="module")
def ocr():  # type: ignore[no-untyped-def]
    from evo_helper.tools.scan_coordinates import make_ocr

    return make_ocr()


def test_the_flight_time_is_actually_readable_from_real_pixels(ocr) -> None:  # type: ignore[no-untyped-def]
    """至少有一套配方能把这张真实裁片读成时长。

    这是那次事故的直接守卫：把 `FLIGHT_RECIPES` 换成原先那套默认
    （3× 不二值化），这条就会红。
    """
    crop = Image.open(FLIGHT_CROP)
    for upscale, threshold in FLIGHT_RECIPES:
        text = ocr(crop, digits=False, upscale=upscale, threshold=threshold)
        if parse_game_duration(text) is not None:
            return
    pytest.fail(f"{len(FLIGHT_RECIPES)} 套配方没有一套读得出时长")


def test_the_recipes_agree_on_the_value(ocr) -> None:  # type: ignore[no-untyped-def]
    """能读出来的那几套必须读成同一个值。

    只要求「有一套读得出」是不够的：一套读成 14 秒、另一套读成 14 分，
    调度器就会按错误的时刻回来收战报，而两条都「成功」了。
    """
    crop = Image.open(FLIGHT_CROP)
    parsed = []
    for upscale, threshold in FLIGHT_RECIPES:
        value = parse_game_duration(ocr(crop, digits=False, upscale=upscale, threshold=threshold))
        if value is not None:
            parsed.append(value)
    assert parsed, "一套都读不出来"
    assert len(set(parsed)) == 1, f"配方之间读数不一致: {parsed}"


def test_the_binarisation_is_what_makes_it_work(ocr) -> None:  # type: ignore[no-untyped-def]
    """不二值化确实读不出来——把事故成因本身钉住。

    没有这条，有人日后「顺手简化」掉 threshold 会一路绿灯回到原点。
    """
    crop = Image.open(FLIGHT_CROP)
    assert parse_game_duration(ocr(crop, digits=False, upscale=3, threshold=None)) is None
