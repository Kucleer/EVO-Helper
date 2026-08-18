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

from datetime import timedelta
from pathlib import Path

import pytest

from evo_helper.domain.report_wait import parse_game_duration
from evo_helper.game.pirate_ui import BRIEFING_FLIGHT_ROI
from evo_helper.tools.pirate_loop import FLIGHT_RECIPES

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

#: 侦察简报上那一行的 ROI 裁片，真值「14秒」（2:137:4，2026-08-10）。
FLIGHT_CROP = Path("var/logs/briefing-flight-scout.png")

#: 整屏现场图 → 画面上那一行写着的飞行时间。
#:
#: 这两张是 2026-08-13 找出来的：`parse_game_duration` 收紧成「部分匹配一律失败」
#: 之后（`3天19时36分7秒` 曾被静默读成 `0:36:07`，生产库 209 发里 66 发中招），
#: 原来那四套配方在这两张上**一套都读不出**，于是它们只能记 NULL、按
#: `UNKNOWN_LINE_HOLD`（90 分钟）占航线。补的四套就是照它们量出来的。
FULL_SHOTS = {
    "var/logs/dump-briefing-unrecognised-182102.png": timedelta(minutes=8, seconds=26),
    "var/logs/dump-briefing-unrecognised-182153.png": timedelta(minutes=8, seconds=28),
}

pytestmark = pytest.mark.skipif(
    not (FLIGHT_CROP.exists() and all(Path(name).exists() for name in FULL_SHOTS)),
    reason=f"缺实拍截图 {FLIGHT_CROP} / var/logs/dump-briefing-unrecognised-1821*.png",
)


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


def _read_flight(ocr, path: str, recipes):  # type: ignore[no-untyped-def]
    """照 `PirateLoop._read_flight_time()` 那条路读：第一个解析成功的算数。"""
    crop = Image.open(path).crop(BRIEFING_FLIGHT_ROI)
    for upscale, threshold in recipes:
        value = parse_game_duration(ocr(crop, digits=False, upscale=upscale, threshold=threshold))
        if value is not None:
            return value
    return None


@pytest.mark.parametrize("path", sorted(FULL_SHOTS))
def test_the_extra_recipes_recover_a_line_the_old_four_could_not_read(ocr, path: str) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 本轮补配方的落点：这两张原先**一套都读不出**，现在读得出，而且读对。

    读不出的代价不是「白跑一趟」而是**一直占着航线**：`expected_report_at_utc` 与
    `line_free_at_utc` 都留 NULL，那一发按 90 分钟算占用，而真实往返是 10–62 分钟。
    """
    assert _read_flight(ocr, path, FLIGHT_RECIPES) == FULL_SHOTS[path]


@pytest.mark.parametrize("path", sorted(FULL_SHOTS))
def test_the_original_four_recipes_really_could_not_read_these(ocr, path: str) -> None:  # type: ignore[no-untyped-def]
    """把「为什么非补不可」钉住：最早那四套在这两张上确实全军覆没。

    没有这条，日后有人把补的四套删掉会一路绿灯——上一条会因为最早那四套里
    某一套碰巧读出来而仍然通过。

    ⚠️ **这四套在这里写死，不再从 `pirate_ui.FLIGHT_RECIPES` 取。** 那个元组
    2026-08-18 又往后追加了六套（复标的依据在它自己的注释里），而这条用例说的
    是「2026-08-13 之前那四套」这个历史事实，取当前值会让它随着每次追加而漂。
    顺带一提：追加的那六套在这两张上也读得对（`(3, None)` 就中），
    所以拿当前值去断言「读不出来」本身已经不成立了。
    """
    original = ((2, 120), (2, 100), (4, 120), (3, 120))

    assert _read_flight(ocr, path, original) is None


@pytest.mark.parametrize("path", sorted(FULL_SHOTS))
def test_no_recipe_produces_a_wrong_value(ocr, path: str) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **这条比「能读出来」更要紧。**

    这个函数取的是**第一个解析成功的**，所以配方表里只要有一套会「成功地读错」，
    排在前面就会把错值写进库。而错值比 NULL 贵得多：它同时污染两个钟
    （战报到点时刻 + 航线空出时刻），还一声不响。

    实测 `nearest` 就是这样的一套：同一张 182102 上 `3×/nearest/140` 把
    `'8分 PEPE'` 解析成 `0:08:00`、`5×/nearest/120` 把 `'as} 6秒'` 解析成
    `0:00:06`。所以 `FLIGHT_RECIPES` 一套 `nearest` 都不许加——这条守的就是它。
    """
    crop = Image.open(path).crop(BRIEFING_FLIGHT_ROI)
    for upscale, threshold in FLIGHT_RECIPES:
        value = parse_game_duration(ocr(crop, digits=False, upscale=upscale, threshold=threshold))
        assert value in (None, FULL_SHOTS[path]), f"{upscale}x/thr{threshold} 读成了 {value}"


def test_nearest_neighbour_is_excluded_because_it_reads_wrong_values(ocr) -> None:  # type: ignore[no-untyped-def]
    """**把「为什么不加 nearest」的凭据本身钉住。**（形式同
    `test_planet_switch_live.py` 里那条「LANCZOS 会把 9 读成 8」。）

    上一条只能证明「现在这几套没读错」——它挡不住有人日后为了多读出几发而顺手
    加一套 `nearest`。这一条说的是加了会怎样：同一块像素上，`nearest` 不是读不出
    （那还安全），而是**成功地读错**。

    这条哪天变绿（tesseract 换了版本、nearest 也读对了），该做的是回来重写
    `FLIGHT_RECIPES` 上那段注释，而不是把它删掉。
    """
    crop = Image.open("var/logs/dump-briefing-unrecognised-182102.png").crop(BRIEFING_FLIGHT_ROI)
    truth = FULL_SHOTS["var/logs/dump-briefing-unrecognised-182102.png"]

    wrong = [
        parse_game_duration(
            ocr(crop, digits=False, upscale=upscale, resample="nearest", threshold=threshold)
        )
        for upscale, threshold in ((3, 140), (5, 120))
    ]

    assert [value for value in wrong if value not in (None, truth)] == [
        timedelta(minutes=8),
        timedelta(seconds=6),
    ]
