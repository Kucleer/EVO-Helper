"""采集军事榜并写入 bot_targets。

横向列边界必须来自实机 V6 标定；本工具故意要求调用者显式传入，避免把未经
标定的猜测固化进源码。它只做导航、读数和入库，绝不打开 allow_actions。
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from evo_helper.config import Settings
from evo_helper.domain.ranking import (
    RankingRow,
    coordinate_of,
    descending_breaks,
    interpolate_scores,
    repair_ranks,
)
from evo_helper.domain.records import RankingTarget
from evo_helper.game.ranking_nav import RankingNavigator, ScrollOutcome, nav_label_words
from evo_helper.game.ranking_ui import RANKING_LIST_MAX_Y, ROW_FIRST_Y, ROW_PITCH_PX
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.tools.scan_coordinates import LiveDriver, SlowDragDriver


@dataclass(frozen=True)
class RankingColumns:
    """V6 标定后传入的三列横向边界（client 空间）。"""

    rank: tuple[int, int]
    name: tuple[int, int]
    score: tuple[int, int]


def parse_score(text: str) -> float | None:
    """解析军事榜的 K/M 缩写；读不出的分数保持 None。"""
    compact = text.strip().upper().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KM])?", compact)
    if match is None:
        return None
    value = float(match.group(1))
    return value * {"K": 1_000.0, "M": 1_000_000.0, None: 1.0}[match.group(2)]


def rows_from_image(image: Any, ocr: Any, columns: RankingColumns) -> list[RankingRow]:
    """按 V6 的列边界逐格 OCR；缺少名次或名字的一行必须丢掉。"""
    rows: list[RankingRow] = []
    index = 0
    while True:
        center = ROW_FIRST_Y + index * ROW_PITCH_PX
        if center > RANKING_LIST_MAX_Y:
            break
        top = round(center - ROW_PITCH_PX / 2)
        bottom = round(center + ROW_PITCH_PX / 2)
        rank_text = _read_cell(image.crop((columns.rank[0], top, columns.rank[1], bottom)), ocr)
        name = _read_cell(image.crop((columns.name[0], top, columns.name[1], bottom)), ocr)
        if not (rank := _rank_of(rank_text)) or not name:
            index += 1
            continue
        score = parse_score(
            _read_cell(image.crop((columns.score[0], top, columns.score[1], bottom)), ocr)
        )
        rows.append(RankingRow(rank=rank, name=name, score=score, coordinate=coordinate_of(name)))
        index += 1
    return rows


def targets_from_rows(rows: list[RankingRow], *, observed_at: datetime) -> list[RankingTarget]:
    """修名次、报告降序异常、插分数，并保留分数是否估算的证据。"""
    repaired = repair_ranks([row.rank for row in rows])
    scores = [row.score for row in rows]
    breaks = descending_breaks(scores)
    if breaks:
        print(f"军力值降序异常行: {breaks}")
    filled = interpolate_scores(scores)
    normalized = [
        RankingRow(
            rank=repaired[index],
            name=row.name,
            score=filled[index],
            coordinate=row.coordinate,
        )
        for index, row in enumerate(rows)
    ]
    return [
        RankingTarget(
            coordinate=row.coordinate,
            military_score=row.score,
            military_score_at_utc=observed_at,
            military_score_estimated=scores[index] is None and row.score is not None,
        )
        for index, row in enumerate(normalized)
        if row.coordinate is not None
    ]


def scan(columns: RankingColumns) -> int:
    """运行一次完整榜单采集；掉线与正常到底严格分开。"""
    import pytesseract

    pytesseract.pytesseract.tesseract_cmd = Settings().tesseract_path
    driver = LiveDriver()  # 默认 False：此工具没有派舰队能力。
    ocr = pytesseract

    def read_rows() -> list[RankingRow]:
        return rows_from_image(driver.capture(), ocr, columns)

    nav = RankingNavigator(
        driver=SlowDragDriver(driver),
        read_labels=lambda: nav_label_words(driver.capture(), ocr),
        read_rows=read_rows,
    )
    collected: list[RankingTarget] = []
    try:
        initial_rows = list(nav.open_military_ranking())
        collected.extend(targets_from_rows(initial_rows, observed_at=datetime.now(UTC)))
        while True:
            step = nav.scroll_once()
            if step.outcome is ScrollOutcome.SCROLLED:
                collected.extend(targets_from_rows(list(step.rows), observed_at=datetime.now(UTC)))
                continue
            if step.outcome is ScrollOutcome.OFF_PAGE:
                print("排行榜已离页（可能断线）；本轮不将半截榜单当作完整结果入库")
                return 2
            break
    finally:
        if not nav.close():
            print("排行榜已关闭，但导航条还原未确认")
    repository = SqlAlchemyRepository(
        create_session_factory(create_database_engine(Settings().database_url))
    )
    repository.save_ranking_targets(collected)
    print(f"军事榜采集完成：写入 {len(collected)} 条榜单目标")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("rank", "name", "score"):
        parser.add_argument(
            f"--{name}-column", nargs=2, type=int, metavar=("LEFT", "RIGHT"), required=True
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    columns = RankingColumns(
        rank=(args.rank_column[0], args.rank_column[1]),
        name=(args.name_column[0], args.name_column[1]),
        score=(args.score_column[0], args.score_column[1]),
    )
    return scan(columns)


def _rank_of(text: str) -> int | None:
    match = re.search(r"\d+", text)
    return int(match.group()) if match is not None else None


def _read_cell(cell: Any, ocr: Any) -> str:
    from PIL import Image

    grey = cell.convert("L").resize((cell.width * 3, cell.height * 3), Image.Resampling.LANCZOS)
    return str(ocr.image_to_string(grey, lang="eng", config="--psm 7")).strip()


__all__ = ["RankingColumns", "main", "parse_score", "rows_from_image", "targets_from_rows"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
