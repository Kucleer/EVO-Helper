"""把「主题读不出」的现场从日志摊平成一张表。

这个工具存在的全部理由是**一个数**：主题读不出的那些行里，有多少是被名义 ROI 的
上沿切掉的（换框救得了），又有多少框本来就是对的（那才轮到换配方）。两者处置相反，
而混在一起看就是 2026-09-12 那一夜的处境——只有一串噪声字符串，怎么改都在猜。

⚠️ 本文件不连库。`_from_database` 是一条只读 SELECT，`--from-file` 就是给离线
复算留的口子（同 `tools.mail_unread_probe`）。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from evo_helper.tools.mail_subject_evidence import (
    EvidenceRow,
    aligned_table,
    offset_table,
    rows_from_logs,
    write_corpus,
)

#: 一个最小的合法 PNG（1×1）。这一层只搬字节，不看画面。
ONE_PIXEL_PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753"
        "de0000000c4944415408d763f8cfc00000030101006039d80000000049454e44ae426082"
    )
).decode("ascii")


def _record(
    log_id: int,
    rows: list[dict[str, Any]],
    extras: list[dict[str, Any]],
    source: str = "scan",
) -> dict[str, Any]:
    return {"id": log_id, "source": source, "rows": rows, "evidence_rows": extras}


def test_the_crops_follow_the_row_index_not_the_position() -> None:
    """⚠️⚠️ **裁片按 `index` 归位，不按下标。**

    `evidence_rows` 只覆盖名额之内的那两行，和 `rows` 不等长。按下标对会把
    第 2 行的裁片安到第 0 行头上——而那种错**不会报错**，只会让人对着别人的
    像素下结论。整件事的教训在 `mail_unread_probe.rows_from_logs` 上。
    """
    rows = rows_from_logs(
        [
            _record(
                7,
                [
                    {"index": 0, "subject": "一一 band", "title_band_offset": 24},
                    {"index": 2, "subject": "~~ ae te", "title_band_offset": 25},
                ],
                [{"index": 2, "mail_row_png_base64": ONE_PIXEL_PNG, "aligned_kind": "ATTACK"}],
            )
        ]
    )

    by_index = {row.index: row for row in rows}
    assert by_index[0].crop_png_base64 == ""
    assert by_index[2].crop_png_base64 == ONE_PIXEL_PNG
    assert by_index[2].aligned_kind == "ATTACK"


def test_a_record_without_rows_is_skipped_whole() -> None:
    """宁可少一条也不要半条——同 `mail_unread_probe.rows_from_logs`。"""
    assert rows_from_logs([{"id": 1}, {"rows": []}, {"id": "x", "rows": [{"index": 0}]}]) == []


def _row(
    offset: int | None,
    *,
    aligned: str | None = None,
    crop: str = "",
    source: str = "scan",
) -> EvidenceRow:
    return EvidenceRow(
        log_id=1,
        index=0,
        source=source,
        subject="一一 band",
        raw_time_text=None,
        unread=True,
        title_band_offset=offset,
        aligned_subject=None if aligned is None else "攻击报告",
        aligned_kind=aligned,
        crop_png_base64=crop,
    )


def test_the_cut_comes_from_the_measured_ink_not_from_a_literal() -> None:
    """⚠️ 分档的切口 = 主题墨迹上沿相对时刻带顶的距离，**不是写死的 30**。

    谁改了 `MAIL_SUBJECT_INK_DY` 而没想起这张表，分档会跟着变而不是悄悄错。
    """
    from evo_helper.vision.optional.report_screens import MAIL_SUBJECT_INK_DY

    cut = -MAIL_SUBJECT_INK_DY[0]

    assert _row(cut - 1).clipped is True
    assert _row(cut).clipped is False
    assert _row(None).clipped is None


def test_the_offset_table_splits_the_two_causes() -> None:
    """三档各自数得出来：被切掉的、框对的、连锚点都没有的。"""
    lines = "\n".join(offset_table([_row(24), _row(25), _row(48), _row(None)]))

    assert "主题读不出的行共 4 行" in lines
    assert "2 行" in lines  # 被切掉的那两行
    assert "24 .. 48" in lines


def test_rows_that_were_never_re_read_stay_out_of_the_denominator() -> None:
    """⚠️ 「没试过」和「试了没救回来」是两件事。

    混在一起会把换框的收益压低——而那个数正是这张表要交出去的东西。
    """
    lines = "\n".join(aligned_table([_row(24, aligned="ATTACK"), _row(25), _row(26)]))

    assert "1 行里认出 1 行（100.0%）" in lines


def test_a_batch_with_no_re_reads_says_so_instead_of_dividing_by_zero() -> None:
    assert "一行都没有" in aligned_table([_row(24), _row(25)])[0]


def test_only_the_rows_that_carry_pixels_land_as_files(tmp_path: Path) -> None:
    """文件名带上 `log_id` 与行号，好和表格对得上。"""
    written = write_corpus([_row(24, crop=ONE_PIXEL_PNG), _row(25)], tmp_path)

    assert [path.name for path in written] == ["subject-1-0.png"]
    assert written[0].read_bytes().startswith(b"\x89PNG")


def test_nothing_to_write_makes_no_directory(tmp_path: Path) -> None:
    """一张图都没有时不留一个空目录——空目录会让人以为语料采过了。"""
    target = tmp_path / "mail-subject"

    assert write_corpus([_row(24), _row(25)], target) == []
    assert not target.exists()


def test_the_two_kinds_of_screen_are_counted_apart() -> None:
    """⚠️⚠️ **切标签嗅探那一档按「趟」算账，正经读那一档按「封」算账。**

    #329 的二级标签判据就是 `row.kind`：主题读不出时舰队类和攻击报告都数到 0
    ⇒ 判「对不上」⇒ 三次之后**整趟放弃**。所以 `sub_tab` 哪怕行数少得多，
    也可能是更该先修的那一边。混在一个总数里，这件事就永远看不出来。
    """
    lines = "\n".join(offset_table([_row(24), _row(25), _row(24, source="sub_tab")]))

    assert "按用途分" in lines
    assert "scan" in lines and "sub_tab" in lines
    assert "整趟放弃" in lines


def test_one_kind_of_screen_needs_no_split() -> None:
    """只有一种来源时不打这张表——没有对比的分档只是噪声。"""
    assert "按用途分" not in "\n".join(offset_table([_row(24), _row(25)]))


def test_a_record_without_a_source_reads_as_a_real_scan() -> None:
    """老记录（这一版之前写的）没有 `source`，按最坏的那一档算：正经读。

    ⚠️ 不许倒向 `sub_tab`：那一档在账上是「嗅探，无所谓」，
    而把真花掉开封预算的行记成嗅探，会让代价整体看着变小。
    """
    rows = rows_from_logs([{"id": 3, "rows": [{"index": 0, "title_band_offset": 24}]}])

    assert [row.source for row in rows] == ["scan"]
