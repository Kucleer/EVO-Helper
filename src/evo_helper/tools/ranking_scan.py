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
from evo_helper.domain.models import Coordinate
from evo_helper.domain.quantities import parse_quantity
from evo_helper.domain.ranking import (
    RankingRow,
    bot_area_reached_message,
    coordinate_of,
    descending_breaks,
    interpolate_scores,
    is_bot_coordinate,
    mentions_bot,
    repair_ranks,
)
from evo_helper.domain.records import RankingTarget
from evo_helper.game.ranking_nav import RankingNavigator, ScrollOutcome, nav_label_words
from evo_helper.game.ranking_ui import (
    BLIND_SCROLL_MARGIN,
    BLIND_SCROLLS,
    DRY_SCREENS,
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
from evo_helper.tools.runner_logging import install_runner_system_log
from evo_helper.tools.scan_coordinates import (
    LiveDriver,
    SlowDragDriver,
    run_with_foreground_guard,
    say,
    warn,
)


@dataclass(frozen=True)
class RankingColumns:
    """三列的横向边界（client 空间）。默认是 2026-08-14 实机量的词框。"""

    rank: tuple[int, int] = RANK_COLUMN
    name: tuple[int, int] = NAME_COLUMN
    score: tuple[int, int] = SCORE_COLUMN


def parse_score(text: str) -> float | None:
    """解析军事榜的 K/M 缩写；读不出的分数保持 None。

    **换算本身下沉到了 `domain.quantities.parse_quantity`**，军力榜与战报的
    「获得资源」共用同一份。两份实现迟早分家，而分家那天不会有人发现：两边都
    「读出了数」，只是其中一边读错了三个数量级。

    ⚠️ **换算必须走 `Decimal`，不能写 `float("64.96") * 1000`。** 后者给出
    `64959.99999999999`——`64.96` 这个十进制小数在二进制里没有精确表示，乘完
    误差就露出来了。榜上读到的原文是 `64.96K`，页面上显示成一串小数尾巴，
    而且这个脏值会**落库**：`bot_targets.military_score` 里已经存了一批。

    实测三个（2026-08-17 页面上的原样）：

        64.96K → 64959.99999999999      64.26K → 64260.00000000001
        64.18K → 64180.00000000001

    `Decimal("64.96") * 1000` 得到 `Decimal("64960.00")`，转回 float 恰好是
    64960.0——十进制字面量按十进制乘，误差根本不产生。

    ⚠️ **不要改成「乘完取整」。** 榜上的 K 值只到两位小数（最小刻度 0.01K = 10）、
    M 值同样两位（0.01M = 10000），取整到个位对这两种确实无害；但解析器
    **也认没有单位的裸数**，而那一支取整就会把 `1.5` 抹成 `2`。`Decimal` 对
    三种单位一视同仁地精确，不需要靠「最小刻度」这个前提兜底。

    插值产生的 `.5`（`domain.ranking.interpolate_scores` 取中点）是**合法值**，
    不是这里的误差，别顺手一起抹掉。

    下沉之后多认两种写法，两种都是**扩张**而不是改口径：`B` 后缀（榜上迟早
    出现，缺一档的下场是整串判为读不出然后静默丢掉），以及千分位分隔的整数
    （`5.388.122` = 5388122，判据见 `domain.quantities` 模块头）。

    这里仍旧交 `float`：`bot_targets.military_score` 是 `Float` 列，而插值出来的
    半数要留住，换成整数会把它抹掉。
    """
    quantity = parse_quantity(text)
    return None if quantity is None else float(quantity.value)


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


def enter_game_exit_code(driver: LiveDriver, ocr: Any, *, attempts: int = 8) -> int:
    """确认画面在游戏里；不在就交给 `SessionKeeper` 走完整条入口序列。

    返回**本趟该用的退出码**：0 = 进去了；否则见
    `scan_coordinates.exit_code_for_unusable_session`（还有关窗重开配额就报
    `EXIT_ENVIRONMENT_BUSY`，配额耗尽才报 1）。

    ⚠️ **不要自己手写这一段。** 2026-08-15 实机：原先这里只认「进入」那一页，
    于是会话掉回 **START 页**时读到全空、如实拒绝，一整趟采集起不来——而画面
    好好的，只差点一下 START。

    `SessionKeeper` 认得 ENTRY / START / 掉线弹窗 / 会话已死 / 服务器维护五种，
    每一种的善后都不一样（点「进入」/ 点 START / 点掉弹窗 / 关窗重开 / 点「知道了」），
    而且那几条都是实机踩出来的。重写一份必然漏，漏掉的那种就是下一次卡整夜的。

    ⚠️ **恢复阶梯要走全，不能只巡检一次就收。** 这里原先只调一次
    `ensure_connected`，读到 `UNKNOWN` 就当场返回失败——而 `UNKNOWN` 多半只是
    上一条链路把游戏停在某个面板上（浮层压着导航条），关掉浮层就好，
    整段道理写在 `scan_coordinates.dismiss_overlays_if_unrecognised`。
    两条链路各走各的阶梯本来就该合并；更要紧的是**退出码判据要求它走全**：
    「还有重开配额」只有在这一轮真的走到过重开那一级时才说明得了问题，
    否则每一轮都在配额满格的状态下报 75，就成了没有尽头的静默空转。
    """
    del attempts  # 重试与等待都由 SessionKeeper 自己管
    from evo_helper.tools.scan_coordinates import (
        dismiss_overlays_if_unrecognised,
        exit_code_for_unusable_session,
        make_ocr,
        make_session_keeper,
        restart_if_still_unusable,
    )

    del ocr
    keeper = make_session_keeper(driver, make_ocr())
    session = keeper.ensure_connected(force=True)
    session = dismiss_overlays_if_unrecognised(session, driver, keeper)
    session = restart_if_still_unusable(session, keeper)
    if session is None or session.ready:
        return 0
    code = exit_code_for_unusable_session(session)
    say(f"进不去游戏：{session.state.value} — {session.detail}（退出码 {code}）")
    return code


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
    """修名次、**丢掉破坏降序的军力值**、插补空缺，并留下「这个数是估算的」的证据。

    ⚠️ **降序异常必须丢，不能只打印。** 2026-08-15 那一夜的教训：库里 30 个 bot
    的军力值飞到 10 万以上（最高 177 万），而每一个除以 100 都精确落回正常区间
    （`17.73K` 读成 `1773K`）——**丢小数点**，不是随机偏差，是整整齐齐的两个数量级。

    榜单按军力降序排，所以「比上一行大」一眼就认得出来，`descending_breaks`
    当时也确实在报——**可我只打印了，没据此丢**。于是 18 个错值进了库，
    又通过插值传染给相邻的 12 个。

    丢的是**分数不是行**：坐标仍然是好的（那 30 个里有 2 个还是坐标扫描验证过的）。
    丢完之后走插值，用上下两个好邻居补一个中点，并标成估算——
    这正是 `interpolate_scores` 存在的意义。
    """
    repaired = repair_ranks([row.rank for row in rows])
    read = [row.score for row in rows]
    trusted = list(read)
    for index in descending_breaks(read):
        trusted[index] = None
    if len(read) != len(trusted) or any(a != b for a, b in zip(read, trusted, strict=True)):
        dropped = [index for index, score in enumerate(trusted) if score is None and read[index]]
        say(f"军力值破坏降序，丢掉这几行的分数（坐标保留）: {dropped}")
    filled = interpolate_scores(trusted)
    return [
        RankingTarget(
            coordinate=row.coordinate,
            military_score=filled[index],
            military_score_at_utc=observed_at,
            # **读到了但被降序判据丢掉**的那些，补回来的值同样是估算——
            # 判据看的是 `trusted` 不是 `read`，否则被丢掉的行会伪装成实读。
            military_score_estimated=trusted[index] is None and filled[index] is not None,
            military_rank=repaired[index],
        )
        for index, row in enumerate(rows)
        if is_bot_coordinate(row.coordinate)
    ]


def take_batch_targets(
    targets: Sequence[RankingTarget], *, seen: set[Coordinate], limit: int | None
) -> list[RankingTarget]:
    """从一屏中取本批尚未见过的目标，且绝不超过配置数量。"""
    picked: list[RankingTarget] = []
    for target in targets:
        if target.coordinate in seen:
            continue
        if limit is not None and len(seen) >= limit:
            break
        seen.add(target.coordinate)
        picked.append(target)
    return picked


def report_bot_area_reached(scrolled: int, *, blind_scrolls: int) -> None:
    """记下这一趟的实测屏数，并在余量被吃掉时喊一声。

    ⚠️ **两件事刻意绑在同一个出口上。** 那句话是自动标定唯一的**样本**
    （`domain.ranking.bot_area_scrolls` 从 `system_log` 里反解它），而告警是这份
    样本唯一能暴露「盲拖是不是已经拖过头」的时刻。摆成两个各自独立的调用点，
    删掉其中任何一个都不会有东西报错。

    ⚠️ **告警补的是自动标定唯一的盲点。** 标定看不出自己拖过头了：拖过头的表现是
    「第一屏检测就看到 bot」，而那和「刚好卡在 bot 起点上」在数据上一模一样——
    两种都记成 `scrolled == blind_scrolls`。真拖过头时，被跳过去的那一批 bot
    不会报错、不会少一条日志，只是**采回来的数静悄悄少一截**。

    所以余量一旦被吃掉就主动喊一声，而不是等用户哪天自己发现数据不对。
    余量还剩 `scrolled - blind_scrolls` 屏；低于 `BLIND_SCROLL_MARGIN` 就报。
    """
    say(bot_area_reached_message(scrolled))
    slack = scrolled - blind_scrolls
    if slack >= BLIND_SCROLL_MARGIN:
        return
    warn(
        f"⚠️ 盲拖余量告急：本趟实测 {scrolled} 屏到达 bot 区，而盲拖了 {blind_scrolls} 屏，"
        f"余量只剩 {slack} 屏（应有 {BLIND_SCROLL_MARGIN} 屏）。"
        "再漂一点盲拖就会拖过 bot 起点，把榜首那批军力最高的 bot 整段跳过去，"
        "而采回来的数只会静悄悄少一截。请检查攻击配置页上的盲拖屏数是不是手填得太大。"
    )


def scan(
    columns: RankingColumns | None = None,
    *,
    blind_scrolls: int = BLIND_SCROLLS,
    human_scrolls: int = 140,
    bot_scrolls: int = 400,
    bot_limit: int | None = None,
) -> int:
    """跑一趟榜单采集。返回 0 = 正常到底，2 = 中途离页（多半断线）。

    ⚠️ **离页也要入库。** 原先这里 `return 2` 排在 `save_ranking_targets` 前面，
    于是断线就把这一趟全扔了——而交接文档写着**断线是预期结果**（2026-08-14
    实机滚到第 473 名就断）。照那个写法，实机上大概率一条都存不下来。

    离页时只丢**最后一屏**：那一屏是在画面已经变了之后读的，可疑；
    它之前那些是画面正常时读到的，和正常到底的那些一样可信。
    """
    import pytesseract

    if bot_limit is not None and bot_limit < 1:
        raise ValueError("bot_limit must be at least 1")
    # 0 合法（「一屏都别盲拖」是最保守的取值），负数不是。
    if blind_scrolls < 0:
        raise ValueError("blind_scrolls must not be negative")
    columns = columns or RankingColumns()
    pytesseract.pytesseract.tesseract_cmd = Settings().tesseract_path
    driver = LiveDriver()  # 默认 False：此工具没有派舰队能力。
    ocr = pytesseract
    release_stuck_mouse(driver)
    entry = enter_game_exit_code(driver, ocr)
    if entry != 0:
        return entry

    player_name = Settings().player_name

    def read_rows() -> list[RankingRow]:
        return rows_from_image(driver.capture(), ocr, columns, player_name=player_name)

    nav = RankingNavigator(
        driver=SlowDragDriver(driver),
        read_labels=lambda: nav_label_words(driver.capture(), ocr),
        read_rows=read_rows,
        # 把导航条那 8 条输出也接到同一个出口上；不注入的话它们只走 `print`，
        # 既没有时刻也进不了 `system_log`。
        say=say,
    )
    repository = SqlAlchemyRepository(
        create_session_factory(create_database_engine(Settings().database_url))
    )
    written = 0
    collected: set[Coordinate] = set()

    def persist(targets: Sequence[RankingTarget]) -> None:
        """**逐屏落库**，不攒到最后一次性写。

        ⚠️ 2026-08-15 实机：原先只在整趟跑完之后写一次，而一趟按预算要跑一个多
        小时。中途被杀（用户 Ctrl+C、调度器抢占、断线）就**整趟全丢**——那一晚
        跑了五十多屏、采到上百个 bot，库里一条都没有。

        入库本身是幂等的（`bot_targets` 上有坐标唯一约束，重扫只更新），
        所以逐屏写不会重复，只是多几次事务——而那点开销和丢一小时的数据比，
        完全不值一提。
        """
        nonlocal written
        if not targets:
            return
        repository.save_ranking_targets(targets)
        written += len(targets)

    def collect(targets: Sequence[RankingTarget]) -> tuple[list[RankingTarget], bool]:
        """只保留本批前 N 个不同 bot；相邻滚屏的重叠行不重复计数。"""
        picked = take_batch_targets(targets, seen=collected, limit=bot_limit)
        persist(picked)
        reached = bot_limit is not None and len(collected) >= bot_limit
        return picked, reached

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
            say(f"盲拖 {blind_scrolls} 屏（那一段必定还是真人），开始检测 bot")

        marker = name_column_text(driver.capture(), ocr, columns)
        while not mentions_bot(marker):
            if scrolled >= human_scrolls:
                say(f"翻满 {human_scrolls} 屏仍没见到 bot；本轮到此为止")
                outcome = 2
                break
            nav.scroll_blind()
            scrolled += 1
            marker = name_column_text(driver.capture(), ocr, columns)
            if not marker:
                # 整条名字列一个字都读不出来 = 已经不在榜单页上（多半断线）。
                # 这同时是「只在刚确认过的画面上按下手指」那条不变式的把关点。
                say(f"翻真人段第 {scrolled} 屏之后名字列全空；已离页")
                outcome = 2
                break
            if scrolled % 10 == 0:
                say(f"  翻真人段 {scrolled} 屏…")
        else:
            # ⚠️ 这一句不只是给人看的：它是**自动标定唯一的实测样本来源**，
            # 而同一个出口还负责在余量被吃掉时报警。别把它拆回一句 `say`。
            report_bot_area_reached(scrolled, blind_scrolls=blind_scrolls)

        # -- 第二段：细读三列 ------------------------------------------------
        if outcome == 0:
            rows = read_rows()
            first, reached_limit = collect(targets_from_rows(rows, observed_at=datetime.now(UTC)))
            screens.append(first)
            if reached_limit:
                say(f"已采够军力攻击批次 {bot_limit} 个 bot；交给攻击任务")
            dry = 0
            for extra in range(1, 0 if reached_limit else bot_scrolls + 1):
                step = nav.scroll_once()
                if step.outcome is ScrollOutcome.OFF_PAGE:
                    say(f"采集第 {extra} 滚之后离页（多半断线）；丢掉最后一屏")
                    outcome = 2
                    break
                rows = list(step.rows)
                fresh, reached_limit = collect(
                    targets_from_rows(rows, observed_at=datetime.now(UTC))
                )
                # ⚠️ **别在 bot 区的边界上提前收工。** 2026-08-15 实机：刚翻到
                # bot 区时那几屏大半还是真人，本来就没几个新 bot，而
                # `SCROLL_STALL_CONFIRMATIONS`(3) 当场就触发了——一趟只写了 2 条，
                # 而 bot 段有四千多个。
                #
                # bot 段里每屏期望 8 个新的（实测），所以连着 `DRY_SCREENS` 屏
                # 一个都没有才算真的到头。跑不满就由 `bot_scrolls` 预算兜底。
                dry = 0 if fresh else dry + 1
                screens.append(fresh)
                say(f"  采集第{extra:>3}滚 本屏 bot {len(fresh)} 连续空屏 {dry}")
                if reached_limit:
                    say(f"已采够军力攻击批次 {bot_limit} 个 bot；交给攻击任务")
                    break
                if dry >= DRY_SCREENS:
                    say(f"连续 {dry} 屏没有新 bot：这一段到头了")
                    break
    finally:
        if not nav.close():
            say("排行榜已关闭，但导航条还原未确认")

    # 离页时最后一屏是画面已经变了之后读的，可疑——但它**已经逐屏写进去了**。
    # `keep_screens` 在这里只用来报数，不再决定写什么：真要把它撤回来得删行，
    # 而删行比留一条可疑记录危险得多（那条记录带着 source='ranking'，本来就标着未验证）。
    kept = keep_screens(screens, off_page=outcome == 2)
    say(
        f"军事榜采集{'（中途离页）' if outcome else '完成'}："
        f"逐屏写入 {written} 条，其中末屏可疑 {written - len(kept)} 条"
    )
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
    parser.add_argument(
        "--forget-scores-above",
        type=float,
        default=None,
        metavar="SCORE",
        help="把高于这个值的军力值清成空（只清分数不删行），然后退出。用来撤回一批已知错读。",
    )
    parser.add_argument(
        "--bot-limit",
        type=int,
        default=None,
        metavar="N",
        help="最多采集 N 个不同 bot，供一轮军力攻击使用",
    )
    parser.add_argument(
        "--blind-scrolls",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"开榜后先无脑拖 N 屏再开始检测 bot；不传就用默认的 {BLIND_SCROLLS} 屏。"
            "宁小勿大：拖多了会越过 bot 起点，把该采的那一段整个跳过去"
        ),
    )
    for name in ("rank", "name", "score"):
        parser.add_argument(
            f"--{name}-column", nargs=2, type=int, metavar=("LEFT", "RIGHT"), default=None
        )
    return parser


def main(argv: list[str] | None = None) -> int:
    # 日志出口。装不上就是空操作，`say()` 照常打到控制台。
    install_runner_system_log()
    args = build_parser().parse_args(argv)
    if args.forget_scores_above is not None:
        repository = SqlAlchemyRepository(
            create_session_factory(create_database_engine(Settings().database_url))
        )
        cleared = repository.forget_implausible_military_scores(above=args.forget_scores_above)
        say(f"已把 {cleared} 行的军力值清空（高于 {args.forget_scores_above:,.0f}）；坐标保留")
        return 0
    default = RankingColumns()

    def pair(raw: list[int] | None, fallback: tuple[int, int]) -> tuple[int, int]:
        return (raw[0], raw[1]) if raw else fallback

    return run_with_foreground_guard(
        lambda: scan(
            RankingColumns(
                rank=pair(args.rank_column, default.rank),
                name=pair(args.name_column, default.name),
                score=pair(args.score_column, default.score),
            ),
            bot_limit=args.bot_limit,
            # 不传就是 `BLIND_SCROLLS` 那个常量本身，不是另写一个「看起来一样」的
            # 数字：默认值只该有一处。
            blind_scrolls=BLIND_SCROLLS if args.blind_scrolls is None else args.blind_scrolls,
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
    "enter_game_exit_code",
    "keep_screens",
    "name_column_text",
    "release_stuck_mouse",
    "main",
    "parse_score",
    "rows_from_image",
    "targets_from_rows",
    "take_batch_targets",
    "track_progress",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
