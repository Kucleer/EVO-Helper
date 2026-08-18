"""「获得资源」12 格，跑在 34 份**真实战报面板**上。语料不在仓库里，缺图就跳过。

## ⚠️ 为什么语料不在仓库里

本仓是**公开仓库**。一份战报面板上写着账号 ID、出发星与目标坐标、逐格的资源
数量——`expected.json` 那份逐格真值更是把数量原样列了 408 个。这些**一律不许进
git**：2026-08-18 有一版把 34 份面板当夹具提交了，只能整个撤回。

所以语料放在 `var/` 下（`.gitignore` 第一条就挡着它），本机有图才跑，CI 里跳过。
字形层面的判据在 `tests/unit/vision/test_resource_digits.py`：那边是拿字模自己
渲染的合成字条，不含任何游戏数据，CI 里照跑。

**两条缺一不可。** 合成的守住「改坏了会红」——字模表撞车、DP 被改坏、判据被
放松，那边先红；实拍的守住「本来就是对的」——真实像素上的准确率，只有真图能
回答，合成图上似然恒等于 1.0，量不出门槛还剩多少余量。

## 语料长什么样、怎么备齐

`var/fixtures/vision/battle_report_panels/` 下：

- ``*.webp`` —— 从生产库 `battle_report_screenshots` **只读**导出的面板图，
  原样的库内字节（520×695），不是重新截的、也不是合成的。
- ``expected.json`` —— ``{文件名: {"cells": [12 格原文]}}``，真值是**人工对着
  放大图逐格核过的**，408 格无一例外。它是这套识别唯一的裁判。

补语料要连生产库，属于**当次授权**才能做的事，这里不提供脚本。

## 这里断言的是统计量，不是逐份的清单

底下三个数（`CORPUS_SIZE` / `PERFECT_PANELS` / `MISREAD_CELLS`）就是判据本身。
**故意不记「哪一份的哪一格读成了什么」**——那等于把资源数量抄进公开仓库。
换来的写法反而更严：读错的格子必须只差**一个字符**，且那一对必须在
`CONFUSABLE_PAIRS` 里。凑巧读错成另一个数量、或者错得面目全非，都过不去。

## 夹具与实机的一处差别

库里存的是**已经裁好的面板**（`ReportLayout.report_panel` = 视口的
`(700, 105)-(1220, 800)`），而实机 OCR 跑在整帧视口上。所以这里把面板贴回视口
坐标系再取 ROI——**不是**把 ROI 平移过去。两者等价，但贴回去的写法让「用的就是
生产那套版面常量」这件事一眼看得见。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evo_helper.domain.battle_resources import GAINED_SLOT_COUNT, parse_resource_grid
from evo_helper.vision.report_layout import LIVE_LAYOUT
from evo_helper.vision.resource_digits import read_resource_cell

PANELS = Path("var/fixtures/vision/battle_report_panels")
TRUTH = PANELS / "expected.json"

#: 语料份数。2026-08-18 那一批，全部 408 格人工核过。
CORPUS_SIZE = 34

#: 逐格全对的份数。
#:
#: ⚠️ **这个数是判据，两个方向都不许悄悄变。** 涨上去说明识别变准了
#: （好事，把这里改大），掉下来说明有东西回归了。老配方在同一批语料上是 5。
PERFECT_PANELS = 29

#: 仍然读错的格子数（408 格里 5 格，1.2%）。老配方那 10 份读得全的里面就错了 5 份。
#:
#: ⚠️ **这张账是承认，不是豁免。** 要再往下压只能提高截图质量：库里存的是
#: WEBP q90 的有损图，实机读的是原始像素，字模匹配在原始像素上只会更准——
#: 但那一半必须实机验证，离线证不了。
MISREAD_CELLS = 5

#: 允许出现的读错形态：`(真值上的字符, 读成了什么)`。
#:
#: 剩下的错全落在这几对形近字上。**不在这张表里的错法一律当回归**——
#: 比如少读一位、或者把 `K` 读成数字（量级凭空掉三个数量级），那些错误
#: 在库里看不出来，正是最该拦下的一类。
CONFUSABLE_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("3", "9"),
        ("5", "9"),
        ("6", "8"),
        ("8", "9"),
    }
)

Image = pytest.importorskip("PIL.Image", reason="需要 Pillow 才能解开语料里的 WEBP")

pytestmark = pytest.mark.skipif(
    not TRUTH.is_file(),
    reason=f"缺实拍语料（{PANELS}/），本机备齐了才跑——它不进仓库",
)

PANEL = LIVE_LAYOUT.report_panel
GRID = LIVE_LAYOUT.resource_grid


def _expected() -> dict[str, list[str]]:
    raw = json.loads(TRUTH.read_text(encoding="utf-8"))
    return {name: list(entry["cells"]) for name, entry in raw.items()}


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


def _read(name: str) -> list[str]:
    board = _viewport(name)
    return [read_resource_cell(_luminance(board, slot)) for slot in range(GRID.slots)]


def _readings() -> dict[str, list[str]]:
    return {name: _read(name) for name in sorted(_expected())}


@pytest.fixture(scope="module")
def corpus() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """（人工真值, 这套读法读出来的）。解 34 份 WEBP 要几秒，整个模块共用一次。"""
    return _expected(), _readings()


class TestTheWholeCorpusReadsThrough:
    def test_the_corpus_is_the_batch_these_numbers_were_measured_on(
        self, corpus: tuple[dict[str, list[str]], dict[str, list[str]]]
    ) -> None:
        """份数对不上，底下那几个数就不代表同一批语料了。"""
        truth, _ = corpus

        assert len(truth) == CORPUS_SIZE
        assert all(len(cells) == GAINED_SLOT_COUNT for cells in truth.values())

    def test_every_panel_gives_all_twelve_cells(
        self, corpus: tuple[dict[str, list[str]], dict[str, list[str]]]
    ) -> None:
        """34 份**一格都不空**——包括当初整块作废的那 29 份。

        这是这次修复的验收判据本身：29 份失败样本里能读全的，从 0 变成 29。
        """
        _, readings = corpus
        empty = {
            name: [slot for slot, text in enumerate(cells) if not text]
            for name, cells in readings.items()
        }

        assert {name: slots for name, slots in empty.items() if slots} == {}

    def test_every_panel_survives_the_all_or_nothing_gate(
        self, corpus: tuple[dict[str, list[str]], dict[str, list[str]]]
    ) -> None:
        """读全了才有资格入库。这条钉的是「读得出」与「进得去」是同一件事。"""
        _, readings = corpus
        voided = [
            name for name, cells in readings.items() if parse_resource_grid(tuple(cells)) is None
        ]

        assert voided == []


class TestTheCellsMatchWhatAHumanRead:
    def test_the_tally_of_cell_perfect_panels_holds(
        self, corpus: tuple[dict[str, list[str]], dict[str, list[str]]]
    ) -> None:
        """逐格全对的份数。老配方是 5，字模匹配是 29。"""
        truth, readings = corpus
        perfect = [name for name, cells in readings.items() if cells == truth[name]]

        assert len(perfect) == PERFECT_PANELS

    def test_the_tally_of_misread_cells_holds(
        self, corpus: tuple[dict[str, list[str]], dict[str, list[str]]]
    ) -> None:
        """408 格里错几格。掉下来是好事（把 `MISREAD_CELLS` 改小），涨上去是回归。"""
        truth, readings = corpus
        wrong = [
            (name, slot)
            for name, cells in readings.items()
            for slot, text in enumerate(cells)
            if text != truth[name][slot]
        ]

        assert len(wrong) == MISREAD_CELLS

    def test_each_misread_is_a_single_confusable_glyph(
        self, corpus: tuple[dict[str, list[str]], dict[str, list[str]]]
    ) -> None:
        """⚠️ 读错的格子必须只差**一个形近字**，别的错法一律当回归。

        长度都变了（少读一位、`K` 被读成数字）意味着切字出了问题，那种错误
        进了库看不出来——量级凭空差三个数量级，串本身却完全合法。
        """
        truth, readings = corpus
        shapes: list[tuple[str, str]] = []
        for name, cells in readings.items():
            for slot, text in enumerate(cells):
                want = truth[name][slot]
                if text == want:
                    continue
                assert len(text) == len(want), f"{name} 第 {slot} 格切字长度都变了"
                differing = [(a, b) for a, b in zip(want, text, strict=True) if a != b]
                assert len(differing) == 1, f"{name} 第 {slot} 格差了不止一个字符"
                shapes.append(differing[0])

        assert set(shapes) <= CONFUSABLE_PAIRS
        assert len(shapes) == MISREAD_CELLS
