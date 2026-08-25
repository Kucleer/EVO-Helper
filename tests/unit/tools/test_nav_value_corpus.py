"""把生产存下来的值框裁片捞成实拍语料。

这个工具是**第三步（重挑配方）的前置**。它本身不做识别，只做搬运——但搬运错了
会静默地把标定引向错误结论，所以真值与图的对应关系要钉死。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from evo_helper.tools.nav_value_corpus import harvest, write_corpus

#: 一个 1×1 的 PNG，base64。内容无所谓——这些用例量的是配对与去重。
PIXEL = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _row(log_id: int, expected: str, position: list[str], *, crops: bool = True) -> dict:
    return {
        "id": log_id,
        "expected": expected,
        "reads": {"galaxy": ["4"] * 5, "system": ["277"] * 5, "position": position},
        **(
            {"value_box_png_base64": dict.fromkeys(("galaxy", "system", "position"), PIXEL)}
            if crops
            else {}
        ),
    }


# -- 挑 --------------------------------------------------------------------------


def test_records_without_crops_are_skipped() -> None:
    """2026-08-25 之前的告警只有整帧缩略图，没有裁片 —— 跳过，别把空条目写进语料。"""
    assert harvest([_row(1, "4:277:15", ["15"] * 5, crops=False)]) == []


def test_the_same_reading_shape_is_only_taken_once() -> None:
    """⚠️⚠️ **按读数形态去重，不按坐标、不按日志 id。**

    生产 430 次告警去重之后只剩 27 种形态，其中 134 次是同一颗星球的同一格。
    不去重的话语料会被少数几颗星球灌满 —— 而这份语料存在的唯一意义是**字形多样性**，
    两次标定翻车都栽在「语料里恰好没有会出错的那几个数」。
    """
    same = ["6", "1", "15", "15", "15"]

    assert len(harvest([_row(1, "4:277:15", same), _row(2, "4:277:15", same)])) == 1


def test_a_different_shape_is_taken_even_from_the_same_coordinate() -> None:
    """⚠️ 反过来：同一颗星球读出了**不一样**的形态，那是新样本，要收。

    这一条和上一条是一对。少了它，去重可能被写成「按坐标去重」——那会把同一颗星球
    上真正不同的字形错法一起丢掉。
    """
    rows = [
        _row(1, "4:277:15", ["6", "1", "15", "15", "15"]),
        _row(2, "4:277:15", ["", "7", "7", "7", "7"]),
    ]

    assert len(harvest(rows)) == 2


# -- 落地 ------------------------------------------------------------------------


def test_each_crop_is_written_next_to_the_value_it_should_read(tmp_path: Path) -> None:
    """⚠️⚠️ **图和真值的配对不许错位。**

    三个框各有各的真值（`4` / `277` / `15`）。拿 `galaxy` 的图去配 `position` 的
    真值不会报错，只会让重挑配方时得出「这套配方读不准」的错误结论 ——
    一个静默地把标定引向反方向的 bug。

    文件名里带上真值，是为了让这件事**在文件系统上就看得见**。
    """
    write_corpus([_row(85907, "4:277:15", ["6", "1", "15", "15", "15"])], tmp_path)

    written = sorted(path.name for path in tmp_path.glob("*.png"))
    assert written == ["85907-galaxy-4.png", "85907-position-15.png", "85907-system-277.png"]


def test_the_png_bytes_land_untouched(tmp_path: Path) -> None:
    """存进去的字节要原样出来 —— 中间任何一道重编码都会改掉要标定的像素。"""
    write_corpus([_row(1, "4:277:15", ["15"] * 5)], tmp_path)

    assert (tmp_path / "1-galaxy-4.png").read_bytes() == base64.b64decode(PIXEL)


def test_the_truth_file_says_it_is_a_draft(tmp_path: Path) -> None:
    """⚠️ 真值取自 `expected`，那是**先验不是铁证** —— 回读对不上也可能是导航栏
    真的停在别处。

    所以每张都进「需人工核对」名单。少了这一句，下一个人会把草稿直接当语料用，
    而错的真值会让重挑配方得出**恰好相反**的结论。
    """
    write_corpus([_row(1, "4:277:15", ["15"] * 5)], tmp_path)

    summary = json.loads((tmp_path / "truth-draft.json").read_text(encoding="utf-8"))
    assert "草稿" in summary["说明"]
    assert sorted(summary["需人工核对"]) == sorted(summary["真值草稿"])


def test_the_reads_of_that_moment_travel_with_the_image(tmp_path: Path) -> None:
    """⚠️ 当时那五套配方读成了什么，要跟着图一起留下。

    重挑配方时第一个问题就是「新配方比老的好在哪」，而那要有老配方在**同一张图**上
    的成绩做对照。分开存等于以后再也对不上号。
    """
    write_corpus([_row(1, "4:277:15", ["6", "1", "15", "15", "15"])], tmp_path)

    summary = json.loads((tmp_path / "truth-draft.json").read_text(encoding="utf-8"))
    assert summary["当时的读数"]["1-position-15.png"] == ["6", "1", "15", "15", "15"]
