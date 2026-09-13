"""把「信箱列表页主题读不出」的现场从生产日志里捞出来，并算出那一问的答案。

## 它要答的那一问

> 主题读不出的那些行，到底是**框把字切了**，还是**那几个像素本来就糊**？

两者处置相反（换框 vs 换配方），而日志里只剩一串噪声字符串时两者分不开。
`pirate_loop.PirateLoop._record_unreadable_subject_evidence` 因此每条记录都带
三样：离网格量、同一帧自对齐重读的结果、原分辨率裁片。这个工具把它们拼成一张表。

## 三步

1. **采**：什么都不用做。这条取证**默认开**（和未读色那条标定探针相反），
   名额由 `MAX_MAIL_SUBJECT_EVIDENCE` / `MAIL_SUBJECT_EVIDENCE_CROPS` 兜住。

2. **算**：

       python -m evo_helper.tools.mail_subject_evidence --out var/fixtures/vision/mail-subject

   打印两张表：按离网格量分档的「名义 ROI 认出几行」，以及自对齐重读的结果。

3. **看**：裁片落成 PNG，**人眼认出那几行本该是什么**。机器读的那两样只说得出
   「读不出」，说不出「本该是舰队返回」——而 2026-09-12 那一夜真正缺的就是它。

## 这个工具不做的事

- **不起游戏、不点鼠标。** 取证是跑任务时顺带记的。
- **不改任何判据。** 同 `tools.nav_value_corpus` / `tools.mail_unread_probe`：
  它只把数字摆出来，换不换框由人决定。
- **不写库。** 只读 SELECT。

## ⚠️ 离网格量那一档怎么读

一行的文字只占时刻带顶的 −30..+9，名义行 ROI 是 0..85（整段实测在
`vision.optional.report_screens.MAIL_SUBJECT_INK_DY`）。所以

- ``偏移 < 30``：主题被 ROI 上沿横着切掉 —— 换框救得了；
- ``偏移 ≥ 30`` 却仍旧读不出：框是对的，问题在别处 —— 这几行才轮到配方那一侧。

这两档的**行数比**就是「换框值不值得」的答案，也是这个工具存在的全部理由。
"""

from __future__ import annotations

import argparse
import base64
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evo_helper.tools.pirate_loop import MAIL_SUBJECT_EVIDENCE_MESSAGE
from evo_helper.tools.scan_coordinates import make_console_encoding_safe


#: 偏移小于这个数就意味着主题被名义 ROI 的上沿切掉了。
#:
#: 它**不是**一个可调参数：等于主题墨迹上沿相对时刻带顶的距离，
#: 也就是 ``-MAIL_SUBJECT_INK_DY[0]``。改那个常量这里跟着变。
def _clipping_offset() -> int:
    from evo_helper.vision.optional.report_screens import MAIL_SUBJECT_INK_DY

    return -MAIL_SUBJECT_INK_DY[0]


@dataclass(frozen=True, slots=True)
class EvidenceRow:
    """一条记录里的一行。`log_id` + `index` 是它在语料里的身份。"""

    log_id: int
    index: int
    subject: str
    raw_time_text: str | None
    unread: bool | None
    title_band_offset: int | None
    aligned_subject: str | None
    aligned_kind: str | None
    crop_png_base64: str

    @property
    def clipped(self) -> bool | None:
        """主题是不是被名义 ROI 的上沿切掉了。偏移没量到就交回 `None`。"""
        if self.title_band_offset is None:
            return None
        return self.title_band_offset < _clipping_offset()

    @property
    def aligned_readable(self) -> bool | None:
        """自对齐重读之后认出来了吗。没重读过就交回 `None`。"""
        if self.aligned_kind is None:
            return None
        return self.aligned_kind != "UNKNOWN"


def rows_from_logs(records: Iterable[dict[str, Any]]) -> list[EvidenceRow]:
    """把 `system_log` 的 payload 摊平成逐行的语料。**读不通的记录整条跳过。**

    ⚠️ **裁片与自对齐重读按 `index` 归位，不按下标。** 它们只覆盖每条记录里
    `MAIL_SUBJECT_EVIDENCE_CROPS` 那两行，和 `rows` 不等长——按下标对会把
    第 0 行的裁片安到别人头上，而那种错事后没人分得清是工具错了还是取证错了。
    """
    rows: list[EvidenceRow] = []
    for record in records:
        log_id = record.get("id")
        payload_rows = record.get("rows")
        if not isinstance(log_id, int) or not isinstance(payload_rows, list):
            continue
        extras = {
            entry["index"]: entry
            for entry in record.get("evidence_rows") or []
            if isinstance(entry, dict) and isinstance(entry.get("index"), int)
        }
        for entry in payload_rows:
            if not isinstance(entry, dict) or not isinstance(entry.get("index"), int):
                continue
            extra = extras.get(entry["index"], {})
            rows.append(
                EvidenceRow(
                    log_id=log_id,
                    index=entry["index"],
                    subject=str(entry.get("subject") or ""),
                    raw_time_text=entry.get("raw_time_text"),
                    unread=entry.get("unread"),
                    title_band_offset=entry.get("title_band_offset"),
                    aligned_subject=extra.get("aligned_subject"),
                    aligned_kind=extra.get("aligned_kind"),
                    crop_png_base64=str(extra.get("mail_row_png_base64") or ""),
                )
            )
    return rows


def write_corpus(rows: Sequence[EvidenceRow], out: Path) -> list[Path]:
    """把带裁片的那几行落成 PNG。文件名带上 `log_id` 与行号，好和表格对得上。"""
    written: list[Path] = []
    if not any(row.crop_png_base64 for row in rows):
        return written
    out.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if not row.crop_png_base64:
            continue
        try:
            raw = base64.b64decode(row.crop_png_base64)
        except ValueError:
            continue
        target = out / f"subject-{row.log_id}-{row.index}.png"
        target.write_bytes(raw)
        written.append(target)
    return written


def offset_table(rows: Sequence[EvidenceRow]) -> list[str]:
    """按「切没切到」分档数行数。**这张表就是换不换框的答案。**"""
    cut = _clipping_offset()
    clipped = [row for row in rows if row.clipped is True]
    clear = [row for row in rows if row.clipped is False]
    unlocated = [row for row in rows if row.clipped is None]
    total = len(rows) or 1
    lines = [
        f"主题读不出的行共 {len(rows)} 行（切口按偏移 < {cut}，"
        f"也就是主题墨迹上沿落在名义 ROI 外面）：",
        f"  偏移 < {cut:<3d}（主题被上沿切掉，换框救得了）  {len(clipped):4d} 行"
        f"  {len(clipped) / total:6.1%}",
        f"  偏移 ≥ {cut:<3d}（框是对的，问题在别处）        {len(clear):4d} 行"
        f"  {len(clear) / total:6.1%}",
        f"  连时刻带都定位不到（自对齐对它无效）          {len(unlocated):4d} 行"
        f"  {len(unlocated) / total:6.1%}",
    ]
    located = [row.title_band_offset for row in rows if row.title_band_offset is not None]
    if located:
        lines.append(f"  偏移的实测范围：{min(located)} .. {max(located)}")
    return lines


def aligned_table(rows: Sequence[EvidenceRow]) -> list[str]:
    """自对齐重读的战绩。**只统计真的重读过的那几行。**

    没重读过的行不能算进分母：它们是被名额挡掉的，而「没试过」和「试了没救回来」
    是两件事，混在一起会把换框的收益压低。
    """
    tried = [row for row in rows if row.aligned_readable is not None]
    if not tried:
        return ["自对齐重读：一行都没有（名额没花出去，或者这批记录来自更早的版本）"]
    saved = [row for row in tried if row.aligned_readable]
    lines = [
        f"自对齐重读（同一帧、同一套配方，只换 ROI 纵向原点）：{len(tried)} 行里认出 "
        f"{len(saved)} 行（{len(saved) / len(tried):.1%}）",
    ]
    for row in tried:
        mark = "认出" if row.aligned_readable else "仍读不出"
        offset = "?" if row.title_band_offset is None else str(row.title_band_offset)
        lines.append(
            f"  #{row.log_id}:{row.index} 偏移 {offset:>4s}  名义读作 {row.subject[:28]!r}"
        )
        lines.append(f"        自对齐 {mark} → {row.aligned_kind} {(row.aligned_subject or '')!r}")
    return lines


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - CLI
    make_console_encoding_safe()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="裁片落到哪个目录")
    parser.add_argument(
        "--from-file", type=Path, help="改从一个 JSON 文件读记录（离线复算用，不连库）"
    )
    args = parser.parse_args(argv)

    records = (
        json.loads(args.from_file.read_text(encoding="utf-8"))
        if args.from_file
        else _from_database()
    )
    rows = rows_from_logs(records)
    written = write_corpus(rows, args.out)
    print(f"日志 {len(records)} 条 → 主题读不出的行 {len(rows)} 行")
    print(f"落地裁片 {len(written)} 张到 {args.out}")
    if not rows:
        print("⚠️ 一行都没有 —— 这一段时间里没有主题读不出的行，或者生产还没跑到这版代码。")
        return 0
    print()
    for line in offset_table(rows):
        print(line)
    print()
    for line in aligned_table(rows):
        print(line)
    print("\n⚠️ 这两张表说的是「框对不对」。**「这几行本该是什么」只有人眼答得了**——")
    print(f"   去 {args.out} 看裁片，那才是换配方值不值得的依据。")
    return 0


def _from_database() -> list[dict[str, Any]]:  # pragma: no cover - 要连库
    """从库里捞。**只读。**连接串走 `Settings()`，同 `tools.mail_unread_probe`。"""
    from sqlalchemy import text

    from evo_helper.config import Settings
    from evo_helper.storage.database import create_database_engine

    sql = text("SELECT id, payload_json FROM system_log WHERE message = :message ORDER BY id")
    records: list[dict[str, Any]] = []
    with create_database_engine(Settings().database_url).connect() as conn:
        for log_id, raw in conn.execute(sql, {"message": MAIL_SUBJECT_EVIDENCE_MESSAGE}):
            try:
                payload = json.loads(raw or "{}")
            except ValueError:
                continue
            records.append({"id": log_id, **payload})
    return records


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
