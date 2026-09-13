"""用真实信箱截图守住那条根因：**主题读不出，是框切的，不是配方糊的。**

事故（2026-09-12 夜）：65 分钟里开封 58 封 → 入库 7 封、白开 50 封，约 8 分钟
白花、占挂机时长的 12%。58 封里 55 封的主题读数连「攻击报告」「海盗」字样都没有，
读成 `'TTT     seesere'` / `'一一 band a | rt rm kar Ae'`。主题闸拦不住，
未读必开那一条就把它们开了，开出来是「舰队返回」。

当时手上的假说是**配方**：框宽 520 太宽、放大只有 2×、不二值化。这个文件量的是
那个假说的对照面，而它**被推翻了**——同一批像素、同一套配方（520 宽、`ocr_upscale`、
不二值化、`--psm 6`），**只把 ROI 的纵向原点换成按这一行自己的时刻带对齐**，
认出的行数就从 22/42 涨到 32/42。

根因：一行的文字只占时刻带顶的 −30..+9（39 像素），全挤在行的上三分之一；
名义行 ROI 高 85、行距 86，空出来的 46 像素**全在文字下方**。也就是说
**向上一个像素的余量都没有**，列表一离网格（实拍上偏移从 +10 到 +82 都有），
主题就被 ROI 上沿横着切掉。

⚠️ **`mail_rows()` 与 `ReportLayout.mail_row` 到这一版为止一个字都没动**：
换不换框要等实机裁片，见
`tools.pirate_loop.PirateLoop._record_unreadable_subject_evidence`。这个文件
守的是**结论本身**——谁哪天改了配方或者常量，这几条会说清收益是不是还在那儿。

真图不进仓库（本仓公开，截图能反推坐标与账号），所以缺图时整个文件跳过——
与 `tests/integration/vision/` 下其余 `*_live.py` 同一条规矩。⚠️ **worktree 里
必然跳过**：实拍只在主仓的 `var/` 下。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evo_helper.tools.pirate_loop import mail_row_from_text
from evo_helper.vision.parsers import ReportKind

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

TESSERACT = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

#: 手挑的信箱**列表页**实拍，2026-09-08 的四屏标定样本 + 2026-09-12 下午的一屏。
#:
#: 挑它们是因为**这几屏的离网格量铺得开**（+10 / +23 / +26 / +38 / +48 / +82），
#: 而离网格量正是被测的那个自变量。全挑对齐的那几屏，这几条会绿得毫无意义。
SCREENS = (
    Path("var/logs/sample-mail-down4-221810.png"),
    Path("var/logs/sample-mailbox-221612.png"),
    Path("var/logs/sample-mailunread-03-221918.png"),
    Path("var/logs/sample-mailunread-04-221919.png"),
    Path("var/logs/sample-mailunread-05-221920.png"),
    Path("var/logs/sample-mailunread-11-221926.png"),
    Path("var/mail-list.png"),
)

pytestmark = pytest.mark.skipif(
    not (all(path.is_file() for path in SCREENS) and Path(TESSERACT).is_file()),
    reason="信箱列表页实拍或 Tesseract 不在（worktree 里必然如此：实拍只在主仓 var/）",
)


def _screens(path: Path):  # type: ignore[no-untyped-def]
    from evo_helper.vision.optional.report_screens import ImageReportScreens
    from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport

    image = crop_to_viewport(Image.open(path).convert("RGB"))
    return ImageReportScreens(
        image, layout_for_viewport(image.width, image.height), tesseract_cmd=TESSERACT
    )


def _sample() -> list[tuple[int | None, ReportKind, ReportKind | None]]:
    """每一行的 `(离网格量, 名义 ROI 判定, 自对齐判定)`。**一屏只读一次。**"""
    rows: list[tuple[int | None, ReportKind, ReportKind | None]] = []
    for path in SCREENS:
        screens = _screens(path)
        offsets = screens.mail_title_band_offsets()
        for index, text in enumerate(screens.mail_rows()):
            aligned = screens.mail_row_aligned(index)
            rows.append(
                (
                    offsets[index],
                    mail_row_from_text(index, text).kind,
                    None if aligned is None else mail_row_from_text(index, aligned).kind,
                )
            )
    return rows


@pytest.fixture(scope="module")
def sample() -> list[tuple[int | None, ReportKind, ReportKind | None]]:
    """OCR 一屏要几百毫秒 × 42 行 × 两个框，整份用例共用一次读数。"""
    return _sample()


def test_the_sample_actually_spans_the_grid(sample) -> None:  # type: ignore[no-untyped-def]
    """先证明**样本本身是有效的**：离网格量铺得开，两档都有货。

    这一条排在最前，因为它守的是别的几条的前提。哪天换了样本图、几屏碰巧全都
    对齐，别的几条会「绿得毫无意义」——而那种绿比红危险。
    """
    from evo_helper.vision.optional.report_screens import MAIL_SUBJECT_INK_DY

    cut = -MAIL_SUBJECT_INK_DY[0]
    offsets = [offset for offset, _nominal, _aligned in sample if offset is not None]

    assert len(sample) >= 36, f"只读出 {len(sample)} 行，样本太薄"
    assert len(offsets) >= 0.9 * len(sample), "大半行连时刻带都定位不到，先看 ROI"
    assert min(offsets) < cut <= max(offsets), f"偏移全挤在一档里（{min(offsets)}..{max(offsets)}）"


def test_a_clipped_subject_is_what_makes_the_row_unreadable(sample) -> None:  # type: ignore[no-untyped-def]
    """⚠️⚠️ **根因本身**：偏移落在切口两侧，名义 ROI 的成绩天差地别。

    2026-09-12 离线量到的是 17/18 对 5/24 —— 而那 5 行还是读到了**下一行**的
    主题（偏移 +10 / +11 时自己的主题整条在框外），只因为同屏主题都一样才没露馅。

    阈值刻意留松（框对的 ≥ 80%、被切的 ≤ 40%）：这里要守的是两档之间那个数量级的
    差距，不是某一版 Tesseract 的具体成绩。
    """
    from evo_helper.vision.optional.report_screens import MAIL_SUBJECT_INK_DY

    cut = -MAIL_SUBJECT_INK_DY[0]
    clipped = [row for row in sample if row[0] is not None and row[0] < cut]
    clear = [row for row in sample if row[0] is not None and row[0] >= cut]

    def read(rows: list[tuple[int | None, ReportKind, ReportKind | None]]) -> float:
        return sum(1 for _o, nominal, _a in rows if nominal is not ReportKind.UNKNOWN) / len(rows)

    assert clipped and clear, "两档得各有货，见上一条"
    assert read(clear) >= 0.8, f"框对的那一档只认出 {read(clear):.0%}，先看 ROI 是不是又漂了"
    assert read(clipped) <= 0.4, f"被切的那一档认出了 {read(clipped):.0%}，根因的方向变了"


def test_aligning_the_roi_buys_rows_that_the_recipe_never_could(sample) -> None:  # type: ignore[no-untyped-def]
    """⚠️⚠️ **只换框，不换配方，认出的行数就涨一截。**

    这一条是整份用例的结论：框宽、放大、不二值化、psm 全部照抄 `mail_rows()`，
    唯一的差别是 ROI 的纵向原点。2026-09-12 量到 22/42 → 32/42。

    阈值定在「至少多认出 5 行」：比噪声（换一档 `MAIL_ROW_ALIGNED_DY` 抖 ±1 行）
    高得多，又不至于把结论钉死在某一个具体数字上。
    """
    nominal = sum(1 for _o, kind, _a in sample if kind is not ReportKind.UNKNOWN)
    aligned = sum(
        1 for _o, _k, kind in sample if kind is not None and kind is not ReportKind.UNKNOWN
    )

    assert aligned >= nominal + 5, f"自对齐 {aligned} 行 vs 名义 {nominal} 行：收益没了"


def test_alignment_does_not_cost_the_screens_that_were_already_fine(sample) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 换框**不许把本来读得出的那一档弄坏**。

    偏移 ≥ 切口的那些行今天就读得好；自对齐对它们本该是同一个框（差几像素）。
    这条守的是「别为了救一半人把另一半推下去」——一个改判据时最常见的失效形态。
    """
    from evo_helper.vision.optional.report_screens import MAIL_SUBJECT_INK_DY

    cut = -MAIL_SUBJECT_INK_DY[0]
    lost = [
        offset
        for offset, nominal, aligned in sample
        if offset is not None
        and offset >= cut
        and nominal is not ReportKind.UNKNOWN
        and (aligned is None or aligned is ReportKind.UNKNOWN)
    ]

    assert len(lost) <= 1, f"框对的那一档被自对齐弄丢了 {len(lost)} 行（偏移 {lost}）"
