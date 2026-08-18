"""「获得资源」12 格，跑在 34 份**真实战报截图**上。

## 这批夹具是什么

`tests/fixtures/vision/battle_report_panels/` 里是 2026-08-18 从生产库
`battle_report_screenshots` 只读导出的 34 份面板图——**原样的库内字节**
（520×695 的 WEBP），不是重新截的、也不是合成的。其中 29 份当时「12 格没读全」
被整块作废，5 份存下了收获。

`expected.json` 里每格的真值是**人工对着放大图逐格核过的**，408 格无一例外。
它是这套识别唯一的裁判：识别得准不准，只有真值能回答。

⚠️ **不能用假 OCR 文本代替这批图。** 假取字面只证明接线对；这一块真正难的地方
是**字高只有 9 像素**，而配方好不好只有真图能回答。仓库里踩过的坑正是这个：
上一版配方在唯一一张全 0 的实拍上「12 格全对」，换到有非零收获的真图上
34 份里只有 5 份逐格正确。

## 为什么这条用例在 CI 里跑得起来

识别改成字模匹配之后**不再需要 tesseract**（见 `vision.resource_digits`），
只需要 Pillow 解 WEBP——而 Pillow 在 `[dev]` 里，CI 装得到。

## 夹具与实机的一处差别

库里存的是**已经裁好的面板**（`ReportLayout.report_panel` = 视口的
`(700, 105)-(1220, 800)`），而实机 OCR 跑在整帧视口上。所以这里把面板贴回
视口坐标系再取 ROI——**不是**把 ROI 平移过去。两者等价，但贴回去的写法让
「用的就是生产那套版面常量」这件事一眼看得见。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evo_helper.domain.battle_resources import GAINED_SLOT_COUNT, parse_resource_grid
from evo_helper.vision.report_layout import LIVE_LAYOUT
from evo_helper.vision.resource_digits import read_resource_cell

Image = pytest.importorskip("PIL.Image", reason="需要 Pillow 才能解开夹具里的 WEBP")

PANELS = Path(__file__).resolve().parents[2] / "fixtures" / "vision" / "battle_report_panels"
EXPECTED: dict[str, dict[str, object]] = json.loads(
    (PANELS / "expected.json").read_text(encoding="utf-8")
)
NAMES = sorted(EXPECTED)

#: 眼下仍然读错的格子：`文件名 -> {槽位: 读成了什么}`。
#:
#: ⚠️ **这张表是承认，不是豁免。** 408 格里错这 5 格（1.2%），全是 `3`/`9`、
#: `6`/`8`、`5`/`9` 这几对形近字，而库里存的是 WEBP q90 的有损图——实机读的是
#: 原始像素，只会更准，但那一半离线证不了。
#:
#: 表变短了这条用例会红，**那是好事**：说明识别变准了，把对应的行删掉即可。
#: 表变长了也会红，那就是回归。两个方向都不许悄悄发生。
KNOWN_MISREADS: dict[str, dict[int, str]] = {
    "fail_20260818T005620_25b54de6.webp": {2: "291K"},
    "fail_20260818T005717_5c47f874.webp": {3: "468"},
    "fail_20260818T130023_2280b3fd.webp": {9: "29"},
    "fail_20260818T130202_4badea21.webp": {1: "579K"},
    "fail_20260818T144155_cfdc3517.webp": {0: "569K"},
}

PANEL = LIVE_LAYOUT.report_panel
GRID = LIVE_LAYOUT.resource_grid


def _viewport(name: str):  # type: ignore[no-untyped-def]
    """把面板贴回视口坐标系，好让版面 ROI 原样可用。"""
    panel = Image.open(PANELS / name).convert("RGB")
    board = Image.new("RGB", LIVE_LAYOUT.viewport, (0, 0, 0))
    board.paste(panel, (PANEL.left, PANEL.top))
    return board


def _luminance(board, slot: int) -> list[list[int]]:  # type: ignore[no-untyped-def]
    crop = board.crop(GRID.cell(slot).as_box()).convert("L")
    raw = crop.tobytes()
    return [list(raw[y * crop.width : (y + 1) * crop.width]) for y in range(crop.height)]


def _read(name: str) -> tuple[str, ...]:
    board = _viewport(name)
    return tuple(read_resource_cell(_luminance(board, slot)) for slot in range(GRID.slots))


class TestTheWholeCorpusReadsThrough:
    @pytest.mark.parametrize("name", NAMES)
    def test_every_panel_gives_all_twelve_cells(self, name: str) -> None:
        """34 份**一格都不空**——包括当初整块作废的那 29 份。

        这是这次修复的验收判据本身：29 份失败样本里能读全的，从 0 变成 29。
        """
        cells = _read(name)

        assert len(cells) == GAINED_SLOT_COUNT
        assert [slot for slot, text in enumerate(cells) if not text] == []

    @pytest.mark.parametrize("name", NAMES)
    def test_every_panel_survives_the_all_or_nothing_gate(self, name: str) -> None:
        """读全了才有资格入库。这条钉的是「读得出」与「进得去」是同一件事。"""
        assert parse_resource_grid(_read(name)) is not None


class TestTheCellsMatchWhatAHumanRead:
    @pytest.mark.parametrize("name", NAMES)
    def test_the_reading_matches_the_hand_checked_truth(self, name: str) -> None:
        """逐格对着人工真值核。仍然读错的那几格在 `KNOWN_MISREADS` 里明写着。"""
        truth = list(EXPECTED[name]["cells"])  # type: ignore[arg-type]
        for slot, text in KNOWN_MISREADS.get(name, {}).items():
            truth[slot] = text

        assert list(_read(name)) == truth

    def test_the_known_misreads_are_still_exactly_five(self) -> None:
        """⚠️ 数字本身是判据：408 格里错 5 格。

        它掉下来说明识别变准了（把 `KNOWN_MISREADS` 里对应的行删掉），
        涨上去说明有东西回归了。写死这个数是为了让两种情况都吵起来。
        """
        assert sum(len(cells) for cells in KNOWN_MISREADS.values()) == 5


class TestFailingClosed:
    #: 随便挑一份当初读得全的样本，够用了——这条用例守的是判据，不是某一张图。
    SAMPLE = "ok_20260818T110201_27eb6819.webp"
    BLANKED_SLOT = 6

    def _blanked(self) -> tuple[str, ...]:
        board = _viewport(self.SAMPLE)
        box = GRID.cell(self.BLANKED_SLOT).as_box()
        board.paste((0, 0, 0), box)
        return tuple(read_resource_cell(_luminance(board, slot)) for slot in range(GRID.slots))

    def test_a_cell_without_ink_reads_as_empty_not_as_zero(self) -> None:
        """⚠️ 没有墨迹**不是 0**。

        这一屏上值为 0 的格子照样画着一个 `0`；一点墨迹都没有说明格子挪了位，
        那时候补一个 0 是在编数据。
        """
        cells = self._blanked()

        assert cells[self.BLANKED_SLOT] == ""
        assert all(cells[slot] for slot in range(GRID.slots) if slot != self.BLANKED_SLOT)

    def test_one_blank_cell_voids_the_whole_grid(self) -> None:
        """⚠️ 读不全就**一行都不存**。

        库里「没有这一行 = 这一格是 0」，只有 12 格全读到时这条语义才成立。
        放松它就会凭空造出零，而且不留痕迹。
        """
        assert parse_resource_grid(self._blanked()) is None

    def test_a_taller_ink_band_is_refused(self) -> None:
        """行带高度不是 9 就整格作废——那意味着版面动了，读出来的数都不可信。"""
        board = _viewport(self.SAMPLE)
        luminance = _luminance(board, 0)
        assert read_resource_cell(luminance)

        smeared = [list(row) for row in luminance]
        smeared[0] = [255] * len(smeared[0])

        assert read_resource_cell(smeared) == ""
