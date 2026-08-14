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
    mentions_bot,
    repair_ranks,
)
from evo_helper.domain.records import RankingTarget
from evo_helper.game.ranking_nav import RankingNavigator, ScrollOutcome, nav_label_words
from evo_helper.game.ranking_ui import (
    BLIND_SCROLLS,
    NAME_COLUMN,
    RANK_COLUMN,
    RANKING_LIST_MAX_Y,
    ROW_CROP_HALF_HEIGHT,
    ROW_FIRST_Y,
    ROW_PITCH_PX,
    SCORE_COLUMN,
    SCROLL_STALL_CONFIRMATIONS,
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


def release_stuck_mouse(driver: LiveDriver) -> None:
    """开工前先松一次鼠标左键。**没按着也松一下，无害。**

    ⚠️ 上一个进程要是被 `Stop-Process` / 任务管理器杀掉的（挂机整夜必然发生：
    断线重启、用户 Ctrl+C、机器休眠），`SlowDragDriver.release()` 不会跑，
    **左键就那么按着留下来了**。实测 2026-08-15 撞到过：那之后第一次 `click()`
    的 mouseDown 成了空操作、mouseUp 被游戏当成松手而不是点击，于是「点 ✕ 关面板」
    没生效，紧接着那一拖落在还开着的面板里，把列表又滚了两行。

    代价还不止于游戏：键按着交还给用户时，整个桌面都在拖东西。

    所以每趟开工先无条件松一次。这比「记得判断上一趟是怎么结束的」可靠。
    """
    driver._gui.mouseUp()  # noqa: SLF001 - 与 SlowDragDriver 同一个理由


def ensure_in_game(driver: LiveDriver, ocr: Any, *, attempts: int = 8) -> bool:
    """确认画面在游戏里；掉回入口页就点一次「进入」。返回「现在在游戏里」。

    ⚠️ **挂机整夜必须有这一步。** 会话闲置会掉回入口页（实测 2026-08-15 闲置四
    小时就掉了），而 `open_military_ranking` 在那种画面上只会抛 `RankingNotReached`
    ——外面的 bat 于是每两分钟重试一次，一整夜空转，什么都采不到。

    ⚠️ **空结果不是证据。** 入口页有淡入动画，那一帧读「进入」会读到空串
    （实测撞到过）。所以逐次重读，读到了才点；八次都读不出来就如实返回 False，
    **绝不朝着一个认不出的画面盲点**。
    """
    from evo_helper.game.ranking_nav import merged_labels, nav_label_words
    from evo_helper.tools.scan_coordinates import (
        ENTRY_BUTTON_ROI,
        ENTRY_BUTTON_TEXT,
        ENTRY_UPSCALE,
        make_ocr,
    )

    read = make_ocr()
    seen: list[str] = []
    for _ in range(attempts):
        if len(merged_labels(nav_label_words(driver.capture(), ocr))) >= 4:
            return True
        text = read(driver.capture().crop(ENTRY_BUTTON_ROI), digits=False, upscale=ENTRY_UPSCALE)
        seen.append(text)
        if ENTRY_BUTTON_TEXT in text:
            left, top, right, bottom = ENTRY_BUTTON_ROI
            driver.click((left + right) // 2, (top + bottom) // 2, label=ENTRY_BUTTON_TEXT)
            break
        driver.wait(1.0)
    else:
        print(f"认不出入口页也不在游戏里，不点；逐次读到 {seen}")
        return False

    # 登录要时间。实测 2026-08-15（有游戏活动、卡顿）用了 111 秒。
    for _ in range(40):
        driver.wait(3.0)
        if len(merged_labels(nav_label_words(driver.capture(), ocr))) >= 4:
            return True
    print("点完「进入」等了两分钟仍没进游戏")
    return False


def name_column_text(image: Any, ocr: Any, columns: RankingColumns | None = None) -> str:
    """把**整条名字列**一次读出来（多行）。翻真人段时只需要这一个。

    ⚠️ 这是「便宜的那一半」：逐格细读一屏要 13 行 × 3 列 = 39 次 OCR，
    而真人段占了整整 73 屏（bot 从第 ~587 名才开始，实测 8 名/滚）。
    在那 73 屏里三列全读是纯浪费——那一段里唯一要回答的问题只有
    「到 bot 区了没有」。
    """
    columns = columns or RankingColumns()
    box = (columns.name[0], ROW_FIRST_Y - 25, columns.name[1], RANKING_LIST_MAX_Y)
    return _read_cell(image.crop(box), ocr, single_line=False)


def is_self_row(name: str, player_name: str) -> bool:
    """这一行是不是**自己**那一行。

    ⚠️ **按名字判，不能按 y 判。** 那一行是吸附的：滚过自己名次之前贴在列表底部
    （y=837），滚过之后跳到**顶部**（y≈254）——而顶部正是「首行变没变」这条
    到底判据看的地方。2026-08-15 实机就是被它骗成「一直没滚动」的。

    用**包含**而不是相等：名字那一格前面常粘上一点噪声（实机读到过
    `', Kucleer'`、`'| Kucleer'`、`': Kucleer'`）。大小写也放宽。
    """
    return bool(player_name) and player_name.casefold() in (name or "").casefold()


def rows_from_image(
    image: Any, ocr: Any, columns: RankingColumns | None = None, *, player_name: str = ""
) -> list[RankingRow]:
    """按实机标定的列边界逐格 OCR；**名字读不出来**的一行才丢掉。

    ⚠️ **判据是名字，不是名次。** 原先这里是「名次或名字缺一就丢」，
    而 2026-08-14 实机第一屏就打脸：**榜首前三名没有名次数字，是奖章图标**，
    于是最强的三行会被整个扔掉。名次是校验和（`repair_ranks` 能从邻居补），
    名字才是这一层唯一的产物——它反解出坐标，决定舰队飞去哪。

    ⚠️ **自己那一行要按名字剔掉**（见 `is_self_row`）——它是吸附的，
    `RANKING_LIST_MAX_Y` 只挡得住它贴底那一档。

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
        if not name or is_self_row(name, player_name):
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


def scan(
    columns: RankingColumns | None = None,
    *,
    blind_scrolls: int = BLIND_SCROLLS,
    human_scrolls: int = 140,
    bot_scrolls: int = 40,
) -> int:
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
    release_stuck_mouse(driver)
    if not ensure_in_game(driver, ocr):
        return 1

    player_name = Settings().player_name

    def read_rows() -> list[RankingRow]:
        return rows_from_image(driver.capture(), ocr, columns, player_name=player_name)

    nav = RankingNavigator(
        driver=SlowDragDriver(driver),
        read_labels=lambda: nav_label_words(driver.capture(), ocr),
        read_rows=read_rows,
    )
    # ⚠️ **开榜放在 try 外面。** 它在读标签行那一步就可能失败，那时面板压根没开，
    # 而 `nav.close()` 会点 `RANKING_CLOSE`(750, 71) ——**在认不出的画面上点击**，
    # 那是这条链路的硬红线。放在外面就没有「记得判断开没开」这回事：
    # 抛出去的时候根本走不到 finally。
    nav.open_military_ranking()

    screens: list[list[RankingTarget]] = []
    outcome = 0
    try:
        # -- 第一段：翻真人段，只问「到 bot 区了没有」 ----------------------
        #
        # 用户口径（2026-08-15）：「你应该不停的滚屏，直到你识别到了 bot 关键字，
        # 这样就可以了，然后再开始取军力」。
        #
        # ⚠️ **这一段刻意不判「滚到底了没有」。** 那条判据建在名次 OCR 上，而名次
        # 恰恰是唯一读不准的一列（榜首的 1–2 位数尤其串），实机 2026-08-15 连着
        # 假阳性四次。而 bot 名字是纯 ASCII、读得稳——把判据换到读得准的信号上，
        # 整段就不需要那条判据了。这一段只靠 `human_scrolls` 兜底。
        # 头 `blind_scrolls` 屏连检测都省了——那一段**必定**还在真人区。
        for _ in range(blind_scrolls):
            nav.scroll_blind()
        scrolled = blind_scrolls
        if blind_scrolls:
            print(f"盲拖 {blind_scrolls} 屏（那一段必定还是真人），开始检测 bot")

        marker = name_column_text(driver.capture(), ocr, columns)
        while not mentions_bot(marker):
            if scrolled >= human_scrolls:
                print(f"翻满 {human_scrolls} 屏仍没见到 bot；本轮到此为止")
                outcome = 2
                break
            nav.scroll_blind()
            scrolled += 1
            marker = name_column_text(driver.capture(), ocr, columns)
            if not marker:
                # 整条名字列一个字都读不出来 = 已经不在榜单页上（多半断线）。
                # 这同时是「只在刚确认过的画面上按下手指」那条不变式的把关点。
                print(f"翻真人段第 {scrolled} 屏之后名字列全空；已离页")
                outcome = 2
                break
            if scrolled % 10 == 0:
                print(f"  翻真人段 {scrolled} 屏…")
        else:
            print(f"翻了 {scrolled} 屏到达 bot 区")

        # -- 第二段：细读三列 ------------------------------------------------
        if outcome == 0:
            rows = read_rows()
            screens.append(targets_from_rows(rows, observed_at=datetime.now(UTC)))
            dry = 0
            for extra in range(1, bot_scrolls + 1):
                step = nav.scroll_once()
                if step.outcome is ScrollOutcome.OFF_PAGE:
                    print(f"采集第 {extra} 滚之后离页（多半断线）；丢掉最后一屏")
                    outcome = 2
                    break
                rows = list(step.rows)
                fresh = targets_from_rows(rows, observed_at=datetime.now(UTC))
                # ⚠️ **停止判据建在 bot 名字上，不在名次上。** 名字是这一层唯一
                # 读得准的东西；连着几屏一个新 bot 都没有，才算这一段跑完了。
                dry = 0 if fresh else dry + 1
                screens.append(fresh)
                print(f"  采集第{extra:>3}滚 本屏 bot {len(fresh)} 连续空屏 {dry}")
                if dry >= SCROLL_STALL_CONFIRMATIONS:
                    print(f"连续 {dry} 屏没有新 bot：这一段到头了")
                    break
    finally:
        if not nav.close():
            print("排行榜已关闭，但导航条还原未确认")

    collected = keep_screens(screens, off_page=outcome == 2)
    repository = SqlAlchemyRepository(
        create_session_factory(create_database_engine(Settings().database_url))
    )
    repository.save_ranking_targets(collected)
    print(f"军事榜采集{'（中途离页）' if outcome else '完成'}：写入 {len(collected)} 条榜单目标")
    return outcome


def progress_mark(rows: Sequence[RankingRow]) -> int:
    """这一屏的进度指针：读得出来的名次的**中位数**。一个都读不出就 0。

    用它当「滚动有没有推进」的判据，而不是「两屏 OCR 输出相不相等」。

    ⚠️ **相等那条几乎永远不成立。** 榜单上大量是中文玩家名（`探险12`、`资源32`），
    而名字列跑的是 `eng`——同一行连读两次就是两个不同的噪声串。于是
    `scroll_once` 的 `EXHAUSTED` 一次都不会触发（2026-08-15 实机 55 滚，零次）。

    ⚠️ **取中位数，不取最大值。** 我先写的是 `max()`，实机 113 秒就自己判成
    「到底了」——名次列会串出高位噪声（当场读到过 `[401]`、`[4781]`、`[1411]`，
    而那一屏真实名次只到 20 左右）。串高一次，`max` 就被顶上去，此后真实推进
    （60 → 70 → 80）永远超不过它，于是连续几次都算「没进展」而提前收工。
    **那正是这条判据要防的事故，用 max 等于自己造了一个。**

    中位数对两侧离群都免疫：一屏十二行里错一两个，中间那个不动。而真实推进
    每滚约 8 名（实测），远大于中位数本身的抖动。
    """
    ranks = sorted(row.rank for row in rows if row.rank is not None)
    return ranks[len(ranks) // 2] if ranks else 0


def track_progress(recent: Sequence[int], mark: int) -> tuple[tuple[int, ...], bool]:
    """吃「最近几屏的进度指针」和这一屏的，吐回新窗口 + 「可以收工了」。

    ⚠️⚠️ **这条判据到今天为止仍然不可靠，别拿它当收工的唯一依据。**

    2026-08-15 实机连着假阳性四次，每次都是「列表明明在滚，判据说到底了」：

        v1 两屏 OCR 逐字节相等   → 反过来：中文玩家名读成噪声，一次都不触发
        v2 相邻两屏比最大名次     → 名次串出 [401]，指针被顶飞，113 秒判到底
        v3 相邻两屏比中位数       → 真实推进约 8 名/滚，指针噪声同量级，信噪比约 1
        v4 跨三屏比中位数（本版） → 榜首那几屏名次是 1–2 位数，OCR 读成
                                   `(7, 14, 1, 7)`，跨窗口照样看不出推进

    **根子是名次列 OCR 在榜首不可靠**，而不是用哪个统计量。三位数名次（`[237]`）
    读得挺稳，一两位数（`[4]`、`[5]`）就串。在这上面叠统计量是治标。

    所以调用方**必须另外带一个预算**（时间或滚动次数）兜底，见 `scan()`。
    本函数只当一个提示：它说到底了，多半值得看一眼；它没说，不代表没到底。

    真要解决，方向是换一个不依赖名次 OCR 的进度信号（比如两屏名字集合的
    **模糊重合率**——滚动了就只剩几行重合，没滚就几乎全重合）。还没做。
    """
    window = (*recent, mark)[-(SCROLL_STALL_CONFIRMATIONS + 1) :]
    if len(window) <= SCROLL_STALL_CONFIRMATIONS:
        return window, False
    return window, mark <= window[0]


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


def _read_cell(cell: Any, ocr: Any, *, single_line: bool = True) -> str:
    """灰度、**不二值化**、3× LANCZOS。单格用 `--psm 7`，整条列用 `--psm 6`。"""
    from PIL import Image

    grey = cell.convert("L").resize((cell.width * 3, cell.height * 3), Image.Resampling.LANCZOS)
    config = "--psm 7" if single_line else "--psm 6"
    return str(ocr.image_to_string(grey, lang="eng", config=config)).strip()


__all__ = [
    "RankingColumns",
    "progress_mark",
    "is_self_row",
    "ensure_in_game",
    "keep_screens",
    "name_column_text",
    "release_stuck_mouse",
    "main",
    "parse_score",
    "rows_from_image",
    "targets_from_rows",
    "track_progress",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
