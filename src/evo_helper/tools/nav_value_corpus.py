"""把生产存下来的**值框原分辨率裁片**捞成实拍语料，供重挑配方用。

## 它补的是哪一个洞

导航栏值框的识别栽过两次，两次都是同一句话：**「这套配方只会这样错」——而那个
结论只在本机 43 张实拍上验过，语料里恰好没有会出错的那几个字形。**

- 2026-08-18：九张里八张的恒星系框都是 `137`（这套字体里最结实的数）→ 上线后
  28 次回读 28 次对不上。
- 2026-08-25：43 张里没有 `15`／`6`／`117`／`261`／`391`／`9` → 汇总规则的最后一条
  判据反过来否决了正确读数，1290 个值框里丢掉 123 个。

两次的成因都不是「规则想错了」，是**手里没有会出错的那些字形的真像素**。

`pirate_loop._value_box_evidence` 现在每遇到一种**没见过的读数形态**就把三个值框
按原分辨率存进 `system_log`。这个工具把它们捞出来落成 PNG，并按 `expected` 生成
一份真值草稿——于是「重挑配方」第一次有了覆盖失败字形的语料。

⚠️ 生成的真值是**草稿，必须人眼过**。`expected` 是那一轮的出发星球坐标，回读对
不上也可能是导航栏真的停在别处；虽然 2026-08-25 那批 1290 格全部落在高置信档
（见 `nav_readback_replay.Cell.confident`），但那是**那一批**的结论，不是保证。
落地时每张都写进 `需人工核对` 名单，核过之后手工挪进正式真值文件。

用法：

    python -m evo_helper.tools.nav_value_corpus --out var/fixtures/vision/nav-values
"""

from __future__ import annotations

import argparse
import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evo_helper.tools.nav_readback_replay import BOX_KEYS, MESSAGE

#: 裁片在 payload 里的键。见 `pirate_loop._value_box_evidence`。
CROPS_KEY = "value_box_png_base64"


def harvest(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """挑出带裁片的记录，**按读数形态去重**，一种形态留一条。

    ⚠️ 去重的键是**读数形态**，不是坐标、也不是日志 id。同一颗星球连着错 134 次
    存的是同一张图，而语料要的是字形多样性——2026-08-25 那 430 次告警去重之后
    只剩 27 种形态，这个比例说明不去重的话语料会被少数几颗星球灌满。
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        crops = row.get(CROPS_KEY)
        if not isinstance(crops, dict) or not crops:
            continue
        reads = row.get("reads") or {}
        shape = "|".join("/".join(reads.get(key, [])) for key in BOX_KEYS)
        if shape in seen:
            continue
        seen.add(shape)
        out.append(row)
    return out


def write_corpus(rows: Sequence[dict[str, Any]], out_dir: Path) -> dict[str, Any]:
    """落地 PNG + 真值草稿，返回一份摘要。

    文件名带上日志 id 与那一位的真值（`85907-system-261.png`），**这样文件名本身
    就说得清它是什么**——排障时不必再回库里对一遍。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    truth: dict[str, str] = {}
    reads_by_file: dict[str, list[str]] = {}
    for row in rows:
        crops = row[CROPS_KEY]
        parts = str(row.get("expected", "")).split(":")
        if len(parts) != len(BOX_KEYS):
            continue
        for key, wanted in zip(BOX_KEYS, parts, strict=True):
            encoded = crops.get(key)
            if not encoded:
                continue
            name = f"{row.get('id', 0)}-{key}-{wanted}.png"
            (out_dir / name).write_bytes(base64.b64decode(encoded))
            truth[name] = wanted
            reads_by_file[name] = list((row.get("reads") or {}).get(key, []))

    summary = {
        "说明": (
            "生产 system_log 的导航栏值框原分辨率裁片。真值取那一轮的 expected，"
            "**是草稿，必须人眼逐张核过再当语料用**。"
        ),
        "需人工核对": sorted(truth),
        "真值草稿": truth,
        "当时的读数": reads_by_file,
    }
    (out_dir / "truth-draft.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把生产的导航栏值框裁片捞成实拍语料")
    parser.add_argument("--out", type=Path, required=True, help="落地目录")
    parser.add_argument("--from-file", type=Path, help="读离线 JSON，不连库")
    args = parser.parse_args(argv)

    if args.from_file:
        rows = json.loads(args.from_file.read_text(encoding="utf-8"))
    else:
        rows = _from_database()

    picked = harvest(rows)
    summary = write_corpus(picked, args.out)
    print(f"日志 {len(rows)} 条 → 带裁片且形态不重复的 {len(picked)} 条")
    print(f"落地 {len(summary['真值草稿'])} 张到 {args.out}")
    if not picked:
        print(
            "⚠️ 一张都没有。裁片是 2026-08-25 才开始存的（pirate_loop._value_box_evidence），"
            "在那之前的告警只有整帧缩略图，值框在上面是 34x8、数不出位数。"
        )
    else:
        print("⚠️ truth-draft.json 里的真值是草稿，逐张核过才能当语料。")
    return 0


def _from_database() -> list[dict[str, Any]]:
    """从库里捞。**只读。**"""
    from sqlalchemy import text

    from evo_helper.config import Settings
    from evo_helper.storage.database import create_database_engine

    sql = text("SELECT id, payload_json FROM system_log WHERE message = :message ORDER BY id")
    rows: list[dict[str, Any]] = []
    with create_database_engine(Settings().database_url).connect() as conn:
        for log_id, raw in conn.execute(sql, {"message": MESSAGE}):
            try:
                payload = json.loads(raw or "{}")
            except ValueError:
                continue
            payload.pop("thumbnail_png_base64", None)
            rows.append({"id": log_id, **payload})
    return rows


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
