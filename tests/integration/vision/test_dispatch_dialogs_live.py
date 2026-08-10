"""用真实弹窗截图守住「三个弹窗只有文字不同」这条判据。

派遣链路上有三个单按钮弹窗，**同一个蓝框、同一个绿 ✓、同一个位置**，
只有中间那行字不同：

    未选择任何战舰               舰队全派出去了     → 停下整轮，等航线
    同时派遣的舰队数量已达上限。   航线占满           → 停下整轮，等航线
    没有可执行的任务。            目标在 8 小时保护期 → **跳过这个目标**，继续下一个

最后一条与前两条**处理方式相反**。把它也当成停轮，一个被保护的目标就能让整轮
空转，而它后面可能还排着一堆能打的。

所以判据只能是**文字**：对那个框做模板匹配，三个弹窗给出的分数完全一样。

截图在 `var/` 下，不进 Git，缺图时整个文件跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evo_helper.game.pirate_ui import (
    DIALOG_LINES_FULL,
    DIALOG_NO_MISSION,
    DIALOG_NO_SHIPS,
    DIALOG_TEXT_ROI,
    snap_dialog,
)

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

#: 两屏实拍（2026-08-10）。视口坐标，1920×879。
NO_MISSION_SHOT = Path("var/logs/dialog-no-mission-viewport.png")
NO_SHIPS_SHOT = Path("var/logs/dialog-no-ships-viewport.png")
LINES_FULL_SHOT = Path("var/logs/dialog-lines-full-viewport.png")

#: 仓库常量是 client 空间，视口图要减掉这条标题栏。
TITLE_BAR_PX = 38

pytestmark = pytest.mark.skipif(
    not (NO_MISSION_SHOT.exists() and NO_SHIPS_SHOT.exists() and LINES_FULL_SHOT.exists()),
    reason="缺实拍截图（var/logs/dialog-*.png）",
)


@pytest.fixture(scope="module")
def ocr():  # type: ignore[no-untyped-def]
    from evo_helper.tools.scan_coordinates import make_ocr

    return make_ocr()


def _read_dialog(image, ocr, **recipe):  # type: ignore[no-untyped-def]
    left, top, right, bottom = DIALOG_TEXT_ROI
    crop = image.crop((left, top - TITLE_BAR_PX, right, bottom - TITLE_BAR_PX))
    return ocr(crop, digits=False, **recipe)


def test_the_no_mission_dialog_is_readable_at_the_default_recipe(ocr) -> None:  # type: ignore[no-untyped-def]
    """默认配方就读得出——这一行是白字压蓝底，不像飞行时间那条绿字。

    钉住这一点是有意义的：调用方要是照着飞行时间那条经验给它也加二值化，
    阈值取高了反而读空（实测 threshold=180 就是空串）。
    """
    image = Image.open(NO_MISSION_SHOT)
    assert DIALOG_NO_MISSION in _read_dialog(image, ocr, upscale=3)


def test_the_roi_is_not_accidentally_reading_the_whole_panel(ocr) -> None:  # type: ignore[no-untyped-def]
    """ROI 只框住那一行，别把面板上别的字也吃进来。

    框大了会把周围的舰船名（殖民船/探测器…）读进来，于是三个弹窗的读数里
    都混着同样的噪声，「按文字区分」就失效了——而它是唯一的区分手段。
    """
    image = Image.open(NO_MISSION_SHOT)
    text = _read_dialog(image, ocr, upscale=3)
    for noise in ("殖民船", "探测器", "裂变者", "起点", "终点"):
        assert noise not in text, f"ROI 吃进了 {noise}：{text!r}"


@pytest.mark.parametrize("upscale", [2, 3, 4])
def test_the_reading_is_stable_across_upscales(upscale: int, ocr) -> None:  # type: ignore[no-untyped-def]
    """换放大倍数不该改变结论。

    仓库栽过一次「ROI 与放大倍数是一对」：`BRIEFING_MISSION_ROI` 原先只有 4×
    读得出，而调用方用的是 3×，四发完全正常的攻击全被闸门拦下。
    """
    image = Image.open(NO_MISSION_SHOT)
    assert DIALOG_NO_MISSION in _read_dialog(image, ocr, upscale=upscale)


def test_the_same_roi_reads_the_other_dialog_too(ocr) -> None:  # type: ignore[no-untyped-def]
    """同一个 ROI 一字不改地读出另一个弹窗——「共用一套框」是实测不是推断。

    这条是整个设计的地基：三个弹窗共用框和按钮，只有文字不同。地基一旦不成立
    （比如某个弹窗的文字换了行位置），就必须给它单独标一个 ROI，而不是继续
    拿这一个去读、读出半行然后当成「认不出」。
    """
    image = Image.open(NO_SHIPS_SHOT)
    assert DIALOG_NO_SHIPS in _read_dialog(image, ocr, upscale=3)


def test_the_two_dialogs_are_told_apart_by_text_alone(ocr) -> None:  # type: ignore[no-untyped-def]
    """两屏读出的文字必须互不包含——文字是唯一的区分手段。

    它们的框、按钮位置、配色完全一样，所以对框做模板匹配给出的分数也一样。
    真要有一个弹窗的文案是另一个的子串，「按文字区分」就会静默失效：
    认成另一个之后，「跳过这个目标」和「停下整轮等航线」会做反。
    """
    no_mission = _read_dialog(Image.open(NO_MISSION_SHOT), ocr, upscale=3)
    no_ships = _read_dialog(Image.open(NO_SHIPS_SHOT), ocr, upscale=3)
    assert DIALOG_NO_SHIPS not in no_mission, f"「没有可执行的任务」那屏读成了 {no_mission!r}"
    assert DIALOG_NO_MISSION not in no_ships, f"「未选择任何战舰」那屏读成了 {no_ships!r}"


def test_the_longest_dialog_is_not_clipped_by_the_roi(ocr) -> None:  # type: ignore[no-untyped-def]
    """ROI 的宽度按**最长**那句留——短的那两句读得好好的不代表框够宽。

    实测踩过：按前两个弹窗（7–8 字）标出来的框，读第三个（14 字）时头尾都被
    裁掉，读成 `'[派遣的舰队数量已达上'`。关键词匹配不上 → 弹窗认不出 →
    runner 停下，但停的理由与真实原因无关。
    """
    text = _read_dialog(Image.open(LINES_FULL_SHOT), ocr, upscale=3)
    assert snap_dialog(text) == DIALOG_LINES_FULL, f"读成了 {text!r}"


@pytest.mark.parametrize(
    ("shot", "expected"),
    [
        (NO_MISSION_SHOT, DIALOG_NO_MISSION),
        (NO_SHIPS_SHOT, DIALOG_NO_SHIPS),
        (LINES_FULL_SHOT, DIALOG_LINES_FULL),
    ],
)
def test_every_dialog_snaps_to_itself(shot: Path, expected: str, ocr) -> None:  # type: ignore[no-untyped-def]
    """三屏各自贴回自己，一个都不许贴错。

    贴错的代价是把两类处理做反：「跳过这个目标」和「停下整轮等航线」。
    """
    assert snap_dialog(_read_dialog(Image.open(shot), ocr, upscale=3)) == expected


def test_a_one_character_misread_still_snaps(ocr) -> None:  # type: ignore[no-untyped-def]
    """差一个字仍要认得出——这不是假设，是实测。

    实机把「派**遣**」读成了「派**遗**」。子串判断在这里直接漏，所以判据
    必须走编辑距离；`snap_mission` 因为「攻击」→「政击」踩过同一个坑。
    """
    assert snap_dialog("同时派遗的舰队数量已达上限。") == DIALOG_LINES_FULL


def test_an_unknown_dialog_is_not_forced_into_a_known_one() -> None:
    """没见过的弹窗要返回 None，而不是硬贴到最像的那个。

    硬贴的话，一个全新的游戏提示会被当成「航线满」处理——停下整轮等一个
    永远不会来的返航，而且不留痕迹。
    """
    assert snap_dialog("服务器维护中，请稍后再试") is None
