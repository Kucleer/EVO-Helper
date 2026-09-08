"""把信箱列表页的**未读色语料**从生产日志里捞出来，并算出一组候选标定。

## 它补的是哪一个洞

⚠️ **实拍图一律不进 Git**，所以「未读那几行到底是什么颜色」在仓库里永远量不出来
——每次游戏版面变了都得**在实机上重采一遍**。而猜一组数出来是这道闸最危险的用法
（把已读判成未读 ⇒ 每趟把开封预算翻倍烧掉，日志上看着一切正常）。

`vision.mail_unread.CALIBRATION` 现在那一组量于 **2026-09-08 的四屏实拍**
（自对齐标题带，未读 4 行 / 已读 20 行，两侧 0.2137 / 0.0000）。哪天
`unread_unknown` 在实机日志上居高不下，就照下面三步重走一遍。

## 三步

1. **采**：在实机上带着标记跑一趟（一趟信箱就够）。

       set EVO_HELPER_MAIL_UNREAD_PROBE=1

   `pirate_loop._record_mail_unread_probe` 会把每屏六行的**原分辨率裁片 + 颜色
   读数**记进 `system_log`（最多 `MAX_MAIL_UNREAD_PROBES` 屏）。
   ⚠️ **落库不落文件**：实机在另一台机器上，本地 `var/logs` 跨机取不到。

2. **看**：把裁片落地，人眼认出哪几行是未读。

       python -m evo_helper.tools.mail_unread_probe --out var/fixtures/vision/mail-unread

3. **算**：把未读那几行报给它，它给出一组候选标定的源码，粘进
   `vision.mail_unread.CALIBRATION`。

       python -m evo_helper.tools.mail_unread_probe --out ... --unread 8891:0,1,2,3 --unread 8934:

   ⚠️ **标签只认人给的**：只有在 `--unread` 里被点到的那几条记录参与标定，
   其中没列出的行算已读。没点到的记录**一行都不参与**——「这一屏我没看过」和
   「这一屏全是已读」是两件事，而把前者当后者会把未读样本掺进已读那一侧。
   `8934:`（冒号后面空着）就是「这一屏我看过，全是已读」。

## 这个工具不做的事

- **不起游戏、不点鼠标。** 采语料那一步是实机上跑任务时顺带录的。
- **不写标定。** 它只把候选打印出来，粘不粘由人决定——同
  `tools.nav_value_corpus` 的「真值是草稿，必须人眼过」。
- **两档之间必须有空档，没有就拒绝出数**（`MailUnreadCalibration.__post_init__`
  也会再拦一次）。分不开时给的是那两组数字，不是一个凑出来的阈值。
"""

from __future__ import annotations

import argparse
import base64
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evo_helper.tools.scan_coordinates import make_console_encoding_safe
from evo_helper.vision.mail_unread import (
    MAIL_UNREAD_PROBE_MESSAGE,
    WARMTH_BUCKET_WIDTH,
    MailRowColor,
    MailUnreadCalibration,
)

#: 裁片在 payload 里的键。见 `pirate_loop._record_mail_unread_probe`。
CROPS_KEY = "mail_row_png_base64"

#: 候选 `min_warmth` 只在**桶边界**上取。
#:
#: 直方图已经把精度收到 `WARMTH_BUCKET_WIDTH` 一档，桶内再细分是假精度：
#: 标定出来的数字复算时对不上，而「对不上」这件事事后没人分得清是标定错了
#: 还是代码改了。
WARMTH_CANDIDATES: tuple[int, ...] = tuple(range(WARMTH_BUCKET_WIDTH, 256, WARMTH_BUCKET_WIDTH))

#: 两档之间留出的空档占落差的比例（两侧各让这么多）。
#:
#: 1/3 是**刻意留得宽**：这组数是在一台机器、一个游戏版面上量出来的，而
#: 「这套配方只会这样错」的结论在本仓翻过两次车（见 `tools.nav_value_corpus`）。
#: 空档窄了，换个版面就会开始给出确定的错答案；宽了只是多几行「判不出」，
#: 而判不出等于退回改动之前的行为。
GAP_MARGIN = 1 / 3


@dataclass(frozen=True, slots=True)
class ProbeRow:
    """探针记录里的一行。`log_id` + `index` 是它在语料里的身份。"""

    log_id: int
    index: int
    subject: str
    raw_time_text: str | None
    color: MailRowColor
    crop_png_base64: str


def rows_from_logs(records: Iterable[dict[str, Any]]) -> list[ProbeRow]:
    """把 `system_log` 的 payload 摊平成逐行的语料。**读不通的记录整条跳过。**

    宁可少一条也不要半条：一条缺了 `warmth_buckets` 的记录进来之后，
    占比一律算成 0.0，而 0.0 在已读那一侧是**合法值**——于是它会悄悄把
    已读那一档的上界往下拽。

    ⚠️ **「定位不到时刻带」的行（`located: false`，`pixels == 0`）正是被这一条
    挡在外面的**，而且必须挡：它们根本没有读数，掺进已读那一侧就是往里塞 0.0。
    裁片那一侧不受影响 —— `position` 是 payload 里的下标，跳过一行不会让
    `mail_row_png_base64` 错位。
    """
    rows: list[ProbeRow] = []
    for record in records:
        log_id = record.get("id")
        payload_rows = record.get("rows")
        crops = record.get(CROPS_KEY) or []
        if not isinstance(log_id, int) or not isinstance(payload_rows, list):
            continue
        for position, entry in enumerate(payload_rows):
            if not isinstance(entry, dict):
                continue
            buckets = entry.get("warmth_buckets")
            pixels = entry.get("pixels")
            if not isinstance(buckets, list) or not isinstance(pixels, int) or pixels <= 0:
                continue
            index = entry.get("index")
            rows.append(
                ProbeRow(
                    log_id=log_id,
                    index=index if isinstance(index, int) else position,
                    subject=str(entry.get("subject") or ""),
                    raw_time_text=entry.get("raw_time_text"),
                    color=MailRowColor(
                        pixels=pixels,
                        warmth_buckets=tuple(int(value) for value in buckets),
                        mean_luminance=int(entry.get("mean_luminance") or 0),
                    ),
                    crop_png_base64=(str(crops[position]) if position < len(crops) else ""),
                )
            )
    return rows


def parse_labels(values: Sequence[str]) -> dict[int, set[int]]:
    """`["8891:0,1,2,3", "8934:"]` → `{8891: {0,1,2,3}, 8934: set()}`。

    键的存在本身就是标签：「这一屏我看过」。空集合是「看过、全是已读」，
    而**不在字典里**是「没看过，别拿它标定」。两者的区别见模块头。
    """
    labels: dict[int, set[int]] = {}
    for value in values:
        head, _sep, tail = value.partition(":")
        if not _sep:
            raise ValueError(f"标签要写成 `日志id:行号,行号`（冒号不能省）：{value!r}")
        log_id = int(head.strip())
        indices = {int(part) for part in tail.split(",") if part.strip()}
        labels.setdefault(log_id, set()).update(indices)
    return labels


@dataclass(frozen=True, slots=True)
class Proposal:
    """一组候选标定，连同「凭什么是这一组」。`calibration` 为 None = 分不开。"""

    calibration: MailUnreadCalibration | None
    note: str
    #: 每个候选 `min_warmth` 下两侧的占比区间，供人自己看一眼。
    spread: tuple[tuple[int, float, float], ...] = ()


def propose_calibration(
    unread: Sequence[MailRowColor],
    read: Sequence[MailRowColor],
    *,
    measured_on: str,
) -> Proposal:
    """在桶边界上挑一档 `min_warmth`，让未读的最小占比和已读的最大占比落差最大。

    挑的是**落差**而不是「准确率」：这一步只有几十行样本，准确率在这个量级上
    分辨不出两个候选；而落差直接回答那个真正要紧的问题——**门槛落在空档里
    还是落在钢丝上**（`OUTCOME_INK_THRESHOLD` 的注释写着同一条：40/60/80
    三档读出来一模一样，所以那个门槛不是调出来的参数）。

    分不开（未读的最小值没有严格大于已读的最大值）时**交回 `None`**，
    并把两侧的数字带出来。此时该做的是补语料或换判据，不是把空档收窄。
    """
    if not unread or not read:
        return Proposal(None, f"两侧都要有样本：未读 {len(unread)} 行、已读 {len(read)} 行")
    spread: list[tuple[int, float, float]] = []
    best: tuple[float, int, float, float] | None = None
    for min_warmth in WARMTH_CANDIDATES:
        low = min(color.warm_share(min_warmth) for color in unread)
        high = max(color.warm_share(min_warmth) for color in read)
        spread.append((min_warmth, low, high))
        if best is None or low - high > best[0]:
            best = (low - high, min_warmth, low, high)
    assert best is not None  # WARMTH_CANDIDATES 非空
    gap, min_warmth, low, high = best
    if gap <= 0:
        return Proposal(
            None,
            "两侧分不开：最好的那一档 "
            f"min_warmth={min_warmth} 上，未读最小占比 {low:.4f} ≤ 已读最大占比 {high:.4f}。"
            "补语料或换判据，别把空档收窄。",
            tuple(spread),
        )
    margin = gap * GAP_MARGIN
    calibration = MailUnreadCalibration(
        min_warmth=min_warmth,
        unread_min_share=round(low - margin, 4),
        read_max_share=round(high + margin, 4),
        measured_on=measured_on,
    )
    return Proposal(
        calibration,
        f"min_warmth={min_warmth}：未读最小 {low:.4f}、已读最大 {high:.4f}，落差 {gap:.4f}",
        tuple(spread),
    )


def calibration_source(calibration: MailUnreadCalibration) -> str:
    """候选标定的源码形态，直接粘进 `vision.mail_unread.CALIBRATION`。"""
    return (
        "CALIBRATION: MailUnreadCalibration | None = MailUnreadCalibration(\n"
        f"    min_warmth={calibration.min_warmth},\n"
        f"    unread_min_share={calibration.unread_min_share},\n"
        f"    read_max_share={calibration.read_max_share},\n"
        f'    measured_on="{calibration.measured_on}",\n'
        ")"
    )


def write_corpus(rows: Sequence[ProbeRow], out_dir: Path) -> list[str]:
    """落地每一行的裁片，文件名自明：`<日志id>-row<行号>.png`。

    文件名带上身份是为了让 `--unread 8891:0,1,2,3` 这个标签能**看着图直接写**
    ——同 `tools.nav_value_corpus.write_corpus` 的那条理由。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for row in rows:
        if not row.crop_png_base64:
            continue
        name = f"{row.log_id}-row{row.index}.png"
        (out_dir / name).write_bytes(base64.b64decode(row.crop_png_base64))
        written.append(name)
    return written


def _table(rows: Sequence[ProbeRow], min_warmth: int) -> list[str]:
    lines = [f"  日志id  行  暖占比(≥{min_warmth})  平均亮度  主题"]
    for row in rows:
        share = row.color.warm_share(min_warmth)
        lines.append(
            f"  {row.log_id:>6}  {row.index:>2}  {share:>14.4f}  {row.color.mean_luminance:>8}"
            f"  {row.subject[:28]!r}"
        )
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    # ⚠️ **必须排在 `parse_args` 之前**，理由整段在那个函数上：帮助文本和这里的
    # 输出都带 `⚠️`（U+26A0），Windows 中文控制台是 GBK，编不出来就当场
    # `UnicodeEncodeError`。**实测**：不装它，`--unread` 那一档跑到最后一句
    # 「这是候选，不是结论」上直接崩，而前面的表已经打完了 —— 看着像跑成功了。
    make_console_encoding_safe()
    parser = argparse.ArgumentParser(description="信箱列表页未读色：捞语料 + 算候选标定")
    parser.add_argument("--out", type=Path, required=True, help="裁片落地目录")
    parser.add_argument("--from-file", type=Path, help="读离线 JSON，不连库")
    parser.add_argument(
        "--unread",
        action="append",
        default=[],
        metavar="日志id:行号,行号",
        help="人眼认出的未读行。冒号后面空着 = 这一屏全是已读。可重复。",
    )
    parser.add_argument(
        "--show-warmth",
        type=int,
        default=WARMTH_CANDIDATES[len(WARMTH_CANDIDATES) // 2],
        help="表格里按这一档算暖占比（只影响显示，不影响标定）",
    )
    args = parser.parse_args(argv)

    records = (
        json.loads(args.from_file.read_text(encoding="utf-8"))
        if args.from_file
        else _from_database()
    )
    rows = rows_from_logs(records)
    written = write_corpus(rows, args.out)
    print(f"日志 {len(records)} 条 → 可用行 {len(rows)} 行")
    print(f"落地裁片 {len(written)} 张到 {args.out}")
    if not rows:
        print(
            "⚠️ 一行都没有。语料要带着 EVO_HELPER_MAIL_UNREAD_PROBE=1 在实机上跑一趟信箱"
            "才会产生（见本模块开头的三步）。"
        )
        return 0
    for line in _table(rows, args.show_warmth):
        print(line)

    labels = parse_labels(args.unread)
    if not labels:
        print(
            "\n下一步：看 PNG 认出未读那几行，再带 --unread 日志id:行号,行号 跑一次"
            "（那一屏全是已读就写 --unread 日志id:）。"
        )
        return 0

    labelled = [row for row in rows if row.log_id in labels]
    unread = [row.color for row in labelled if row.index in labels[row.log_id]]
    read = [row.color for row in labelled if row.index not in labels[row.log_id]]
    print(f"\n参与标定：{len(labelled)} 行（未读 {len(unread)} / 已读 {len(read)}）")
    proposal = propose_calibration(
        unread, read, measured_on=f"{sorted(labels)} · {len(labelled)} 行"
    )
    print(proposal.note)
    if proposal.calibration is None:
        for min_warmth, low, high in proposal.spread:
            print(f"  min_warmth={min_warmth:>3}：未读最小 {low:.4f}、已读最大 {high:.4f}")
        return 1
    print("\n⚠️ 这是候选，不是结论。粘进 vision/mail_unread.py 之前请人眼过一遍裁片。\n")
    print(calibration_source(proposal.calibration))
    return 0


def _from_database() -> list[dict[str, Any]]:
    """从库里捞。**只读。**连接串走 `Settings()`，同 `tools.nav_value_corpus`。"""
    from sqlalchemy import text

    from evo_helper.config import Settings
    from evo_helper.storage.database import create_database_engine

    sql = text("SELECT id, payload_json FROM system_log WHERE message = :message ORDER BY id")
    records: list[dict[str, Any]] = []
    with create_database_engine(Settings().database_url).connect() as conn:
        for log_id, raw in conn.execute(sql, {"message": MAIL_UNREAD_PROBE_MESSAGE}):
            try:
                payload = json.loads(raw or "{}")
            except ValueError:
                continue
            records.append({"id": log_id, **payload})
    return records


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
