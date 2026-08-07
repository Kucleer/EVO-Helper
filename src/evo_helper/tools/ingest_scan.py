"""把一次坐标扫描的结果写入数据库。

输入是扫描器产出的 JSON：每个坐标一条，含请求坐标、OCR 读回的坐标、行星名。

**坐标不一致的条目一律不入库。** 面板上读回的坐标必须与请求的坐标逐段相等，
否则说明跳转没生效或读错了——把它当成有效扫描会给错误的坐标记上错误的归属。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import CoordinateScan
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository

#: 玩家名以此开头即判定为 bot（方案第 2 节）。
BOT_PREFIX = "bot_"

#: 系统占位行星，不是玩家，也不是可攻击目标。
UNOWNED_NAMES = {"荒芜行星", "未知", ""}


def digits_of(text: str) -> str:
    """取出文本里的数字序列。

    坐标里的冒号又细又矮，OCR 会漏读（``[2:122:9]`` 读成 ``[2122:9]``）。
    但我们不是在自由解析坐标——我们在核对面板显示的是否**就是请求的那个**坐标。
    数字序列相等即可证明这一点，且对漏读分隔符免疫。
    """
    return "".join(ch for ch in text if ch.isdigit())


def coordinate_confirmed(requested: str, raw_text: str) -> bool:
    return bool(raw_text) and digits_of(raw_text) == digits_of(requested)


def parse_coordinate(text: str) -> Coordinate:
    galaxy, system, position = (int(part) for part in text.split(":"))
    return Coordinate(galaxy, system, position)


#: bot 名形如 ``bot_<银河>_<恒星>_<行星>``，`bot_` 之后只有数字和下划线。
#: OCR 在这种小字号上会把 1 读成 l、2 读成 e、0 读成 O。
_BOT_DIGIT_FIX = {"l": "1", "I": "1", "e": "2", "O": "0", "o": "0", "S": "5", "B": "8"}


def is_bot_name(name: str | None) -> bool:
    return bool(name) and str(name).startswith(BOT_PREFIX)


def normalise_bot_name(name: str) -> str:
    """把 bot 名后半段里被误读成字母的数字还原。

    只在 ``bot_`` 前缀之后动手，且只替换已知的混淆字符——前缀本身不碰，
    因为 bot 判定就靠它，改前缀等于改判定结果。
    """
    if not is_bot_name(name):
        return name
    head, tail = name[: len(BOT_PREFIX)], name[len(BOT_PREFIX) :]
    return head + "".join(_BOT_DIGIT_FIX.get(ch, ch) for ch in tail)


def owner_of(name: str | None) -> str | None:
    """占位行星没有归属；返回 None 而不是把「荒芜行星」当成玩家名。"""
    if name is None or name.strip() in UNOWNED_NAMES:
        return None
    return name.strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True, help="扫描结果 JSON")
    parser.add_argument("--run-id", required=True, help="运行实例 UUID")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = parser.parse_args(argv)

    rows = json.loads(args.results.read_text(encoding="utf-8"))
    run_id = UUID(args.run_id)
    now = datetime.now(UTC)

    accepted: list[CoordinateScan] = []
    rejected: list[dict[str, object]] = []
    for row in rows:
        if not coordinate_confirmed(row["requested"], row.get("raw_coordinate_text", "")):
            rejected.append(row)
            continue
        # 有主布局的名字在 owner，无主布局在 planet_name。早先只读了后者，
        # 导致所有 bot 都被当成空位漏掉。
        name = owner_of(row.get("owner") or row.get("planet_name"))
        if name is not None:
            name = normalise_bot_name(name)
        accepted.append(
            CoordinateScan(
                run_id=run_id,
                coordinate=parse_coordinate(row["requested"]),
                scanned_at_utc=now,
                owner_name=name,
                is_bot=is_bot_name(name),
                # 坐标经过「请求 vs 读回」双向一致校验，故为 1.0。
                confidence=1.0,
            )
        )

    bots = [scan for scan in accepted if scan.is_bot]
    owned = [scan for scan in accepted if scan.owner_name and not scan.is_bot]
    print(f"读入   : {len(rows)}")
    print(f"已接受 : {len(accepted)}")
    print(f"已拒绝 : {len(rejected)}（坐标与请求不一致）")
    print(f"bot    : {len(bots)}")
    print(f"有归属 : {len(owned)}")
    for scan in bots:
        print(f"  BOT {scan.coordinate} {scan.owner_name}")
    for row in rejected:
        print(f"  拒绝 {row['requested']} 原文 {row.get('raw_coordinate_text')!r}")

    if args.dry_run:
        print("dry run：未写入")
        return 0

    repository = SqlAlchemyRepository(
        create_session_factory(create_database_engine(Settings().database_url))
    )
    for scan in accepted:
        repository.save_scan(scan)
    print(f"已写入 {len(accepted)} 条坐标扫描")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
