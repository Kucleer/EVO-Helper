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

# 判定规则只有一份，扫描器和这里共用——各留一份的坑已经踩过。
from evo_helper.vision.scan_reading import (
    BOT_PREFIX,
    UNOWNED_NAMES,
    coordinate_confirmed,
    digits_of,
    is_bot_name,
    normalise_bot_name,
    owner_of,
)

__all__ = [
    "BOT_PREFIX",
    "UNOWNED_NAMES",
    "coordinate_confirmed",
    "digits_of",
    "is_bot_name",
    "main",
    "normalise_bot_name",
    "owner_of",
    "parse_coordinate",
]


def parse_coordinate(text: str) -> Coordinate:
    galaxy, system, position = (int(part) for part in text.split(":"))
    return Coordinate(galaxy, system, position)


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
