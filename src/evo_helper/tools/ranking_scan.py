"""采集军事榜并写入 bot_targets。

列边界来自 2026-08-14 实机标定（`game.ranking_ui.RANK_COLUMN` 等），命令行
可以覆盖。原先这里要求必填，是因为那时还没标定——现在标定了，默认值就是实测值。
它只做导航、读数和入库，绝不打开 allow_actions。
"""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence
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
from evo_helper.game.ranking_ui import (
    NAME_COLUMN,
    RANK_COLUMN,
    RANKING_LIST_MAX_Y,
    ROW_CROP_HALF_HEIGHT,
    ROW_FIRST_Y,
    ROW_PITCH_PX,
    SCORE_COLUMN,
)
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.tools.scan_coordinates import LiveDriver, SlowDragDriver


@dataclass(frozen=True)
class RankingColumns:
    """三列的横向边界（client 空间）。默认是 2026-08-14 实机量的词框。"""

    rank: tuple[int, int] = RANK_COLUMN
    name: tuple[int, int] = NAME_COLUMN
    score: tuple[int, int] = SCORE_COLUMN


def parse_score(text: str) -> float | None:
    """解析军事榜的 K/M 缩写；读不出的分数保持 None。"""
    compact = text.strip().upper().replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KM])?", compact)
    if match is None:
        return None
    value = float(match.group(1))
    return value * {"K": 1_000.0, "M": 1_000_000.0, None: 1.0}[match.group(2)]


def rows_from_image(
    image: Any, ocr: Any, columns: RankingColumns | None = None
) -> list[RankingRow]:
    """按实机标定的列边界逐格 OCR；**名字读不出来**的一行才丢掉。

    ⚠️ **判据是名字，不是名次。** 原先这里是「名次或名字缺一就丢」，
    而 2026-08-14 实机第一屏就打脸：**榜首前三名没有名次数字，是奖章图标**，
    于是最强的三行会被整个扔掉。名次是校验和（`repair_ranks` 能从邻居补），
    名字才是这一层唯一的产物——它反解出坐标，决定舰队飞去哪。

    ⚠️ **裁剪半高比行距的一半窄。** 星球地表的 `TOTAL CREWS` / `COMMAND OFFICERS`
    透过半透明面板落在 x 769–949（正压在名字列上），y 恰好在两行之间：
    真实行 525，背景在 500 和 548。按 `ROW_PITCH_PX / 2` = 22.4 裁会把上下背景
    各吃进去一点，所以用 `ROW_CROP_HALF_HEIGHT`。
    """
    columns = columns or RankingColumns()
    rows: list[RankingRow] = []
    index = 0
    while True:
        center = ROW_FIRST_Y + index * ROW_PITCH_PX
        if center > RANKING_LIST_MAX_Y:
            break
        top = round(center - ROW_CROP_HALF_HEIGHT)
        bottom = round(center + ROW_CROP_HALF_HEIGHT)
        name = _read_cell(image.crop((columns.name[0], top, columns.name[1], bottom)), ocr)
        if not name:
            index += 1
            continue
        rank_box = (columns.rank[0], top, columns.rank[1], bottom)
        rank = _rank_of(_read_cell(image.crop(rank_box), ocr))
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


def scan(columns: RankingColumns | None = None) -> int:
    """跑一趟榜单采集。返回 0 = 正常到底，2 = 中途离页（多半断线）。

    ⚠️ **离页也要入库。** 原先这里 `return 2` 排在 `save_ranking_targets` 前面，
    于是断线就把这一趟全扔了——而交接文档写着**断线是预期结果**（2026-08-14
    实机滚到第 473 名就断）。照那个写法，实机上大概率一条都存不下来。

    离页时只丢**最后一屏**：那一屏是在画面已经变了之后读的，可疑；
    它之前那些是画面正常时读到的，和正常到底的那些一样可信。
    """
    import pytesseract

    columns = columns or RankingColumns()
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
    # ⚠️ **开榜放在 try 外面。** 它在读标签行那一步就可能失败，那时面板压根没开，
    # 而 `nav.close()` 会点 `RANKING_CLOSE`(750, 71) ——**在认不出的画面上点击**，
    # 那是这条链路的硬红线。放在外面就没有「记得判断开没开」这回事：
    # 抛出去的时候根本走不到 finally。
    initial = list(nav.open_military_ranking())

    screens: list[list[RankingTarget]] = [targets_from_rows(initial, observed_at=datetime.now(UTC))]
    outcome = 0
    try:
        previous_top = initial[0].name if initial else ""
        scrolls = 0
        while True:
            step = nav.scroll_once()
            scrolls += 1
            if step.outcome is ScrollOutcome.OFF_PAGE:
                print(f"第 {scrolls} 次滚动后离页（多半断线）；丢掉最后一屏，其余照常入库")
                outcome = 2
                break
            rows = list(step.rows)
            # 打点：滚动到底是不是真的在推进，实机上要盯的就是这两个数
            top = rows[0].name if rows else ""
            ranks = [row.rank for row in rows if row.rank]
            print(
                f"  第{scrolls:>3}滚 名次 {min(ranks) if ranks else '?'}"
                f"-{max(ranks) if ranks else '?'} 首行 {previous_top!r}->{top!r} "
                f"{'（没动）' if top == previous_top else ''}"
            )
            previous_top = top
            screens.append(targets_from_rows(rows, observed_at=datetime.now(UTC)))
            if step.outcome is ScrollOutcome.EXHAUSTED:
                break
    finally:
        # ⚠️ 只有真的开出面板才收尾。`open_military_ranking` 在读标签行那一步就
        # 失败时面板压根没开，这时点 `RANKING_CLOSE`(750, 71) 就是**在认不出的
        # 画面上点击**——那是这条链路的硬红线。
        if not nav.close():
            print("排行榜已关闭，但导航条还原未确认")

    collected = keep_screens(screens, off_page=outcome == 2)
    repository = SqlAlchemyRepository(
        create_session_factory(create_database_engine(Settings().database_url))
    )
    repository.save_ranking_targets(collected)
    print(f"军事榜采集{'（中途离页）' if outcome else '完成'}：写入 {len(collected)} 条榜单目标")
    return outcome


def keep_screens(
    screens: Sequence[Sequence[RankingTarget]], *, off_page: bool
) -> list[RankingTarget]:
    """把逐屏采到的合成一份要入库的清单；离页时**只丢最后一屏**。

    ⚠️ **离页不等于这一趟白跑。** 断线是预期结果（交接文档写着 2026-08-14 实机
    滚到第 473 名就断），全丢的话实机上大概率一条都存不下来。

    只丢最后一屏：那一屏是在画面已经变了之后读的，可疑；它之前那些是画面正常时
    读到的，和正常到底的那些一样可信。
    """
    kept = list(screens[:-1]) if off_page and screens else list(screens)
    return [target for screen in kept for target in screen]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("rank", "name", "score"):
        parser.add_argument(
            f"--{name}-column", nargs=2, type=int, metavar=("LEFT", "RIGHT"), default=None
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    default = RankingColumns()

    def pair(raw: list[int] | None, fallback: tuple[int, int]) -> tuple[int, int]:
        return (raw[0], raw[1]) if raw else fallback

    return scan(
        RankingColumns(
            rank=pair(args.rank_column, default.rank),
            name=pair(args.name_column, default.name),
            score=pair(args.score_column, default.score),
        )
    )


def _rank_of(text: str) -> int | None:
    match = re.search(r"\d+", text)
    return int(match.group()) if match is not None else None


def _read_cell(cell: Any, ocr: Any) -> str:
    from PIL import Image

    grey = cell.convert("L").resize((cell.width * 3, cell.height * 3), Image.Resampling.LANCZOS)
    return str(ocr.image_to_string(grey, lang="eng", config="--psm 7")).strip()


__all__ = [
    "RankingColumns",
    "keep_screens",
    "main",
    "parse_score",
    "rows_from_image",
    "targets_from_rows",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
