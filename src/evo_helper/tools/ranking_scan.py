"""采集军事榜并写入 bot_targets。

列边界来自 2026-08-14 实机标定（`game.ranking_ui.RANK_COLUMN` 等），命令行
可以覆盖。原先这里要求必填，是因为那时还没标定——现在标定了，默认值就是实测值。
它只做导航、读数和入库，绝不打开 allow_actions。
"""

from __future__ import annotations

import argparse
import difflib
import pathlib
import re
import statistics
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate
from evo_helper.domain.quantities import parse_quantity
from evo_helper.domain.ranking import (
    RankingRow,
    bot_area_reached_rows_message,
    coordinate_of,
    interpolate_scores,
    is_bot_entry,
    mentions_bot,
    repair_ranks,
    screens_overlap,
    trusted_scores,
)
from evo_helper.domain.records import RankingTarget
from evo_helper.domain.scheduler import EXIT_RANKING_INCOMPLETE
from evo_helper.game.ranking_nav import (
    RankingNavigator,
    ScrollOutcome,
    SpinResult,
    nav_label_words,
)
from evo_helper.game.ranking_ui import (
    BLIND_SCROLL_MARGIN_ROWS,
    BLIND_SCROLL_ROWS,
    BLIND_SCROLLS,
    BOT_DETECTION_BUDGET_SCROLLS,
    DRY_SCREENS,
    GLIDE_SETTLE_S,
    NAME_COLUMN,
    NAME_SAMPLE_CHARS,
    NAME_SAMPLE_EVERY_SCROLLS,
    NAME_SAMPLE_STUCK_OVERLAP,
    RANK_COLUMN,
    RANK_STRIP_PAD_PX,
    RANK_STRIP_TOP_PAD_PX,
    RANKING_LIST_MAX_Y,
    READ_ATTEMPTS,
    REREAD_WAIT_S,
    ROW_CROP_HALF_HEIGHT,
    ROW_FIRST_Y,
    ROW_GRID_TOLERANCE_PX,
    ROW_PITCH_PX,
    ROW_WORD_SPREAD_PX,
    ROWS_PER_NOTCH,
    ROWS_PER_SCREEN,
    ROWS_PER_SCROLL,
    SCORE_ANCHOR_RESET_SCREENS,
    SCORE_COLUMN,
    SCROLL_STALL_CONFIRMATIONS,
    SPIN_MARK_MIN_ROWS,
)
from evo_helper.infrastructure.system_log import record_system_log
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


def renderable_score(score: float | None) -> bool:
    """这个军力值，游戏**渲染得出来**吗？渲染不出来的一律是 OCR 多插了一位。

    军事榜把军力显示成 `10.29K` / `9.86K` / `1.15M`——**两位小数**。
    所以 1000 以上的值最小刻度是 `0.01K = 10`，**必然是 10 的整数倍**。
    而 `parse_quantity` 也认裸数（那是给别处用的，见它的模块头），于是
    `10.259K` 这种读数被当成合法的 10259 一路放进库里。

    2026-08-23 语料实测，落库的军力值有 8.3% 长这样：

        图上 10.29K → 读成 10.259K → 10259      图上 9.93K → 读成 9.935K → 9935
        图上  9.83K → 读成  3.835K →  3835      图上 9.94K → 读成 5.954K → 5954

    ⚠️ **这一道和 `descending_breaks` 是互补的，不是重复的。** 降序判据认得出
    「比上一行大」；上面这四个**全都比上一行小**，它一个都抓不到。反过来
    「丢小数点」那类（`17.73K` 读成 `1773K`）值飞高，降序判据抓得住而这一道抓不住
    （177300 也是 10 的整数倍）。两道网挡的是两个方向。

    ⚠️ **1000 以下不查。** 那一档游戏直接显示整数（`850` 就是 850），没有小数位，
    任何整数都渲染得出来。榜尾的 bot 就在这一档，拿「10 的整数倍」去查会把
    合法值误丢成估算值。

    ⚠️ **`None` 是合法的**（读不出）——判据是「读出来的数不可能」，
    不是「必须读出来」。军力值本来就允许读不出（用户口径 2026-08-14）。

    ⚠️ **插值出来的 `.5` 不走这里。** `targets_from_rows` 是在插值**之前**查的，
    那时候还没有 `.5`。别把这道判据挪到插值后面去。
    """
    if score is None or abs(score) < 1000:
        return True
    return abs(score) % 10 < 1e-6


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
        wait_for_login_if_unrecognised,
    )

    del ocr
    keeper = make_session_keeper(driver, make_ocr())
    session = keeper.ensure_connected(force=True)
    session = dismiss_overlays_if_unrecognised(session, driver, keeper)
    session = wait_for_login_if_unrecognised(session, keeper)
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


def position_from_image(image: Any, ocr: Any, columns: RankingColumns | None = None) -> int | None:
    """现在滚到第几名了：把**名次那一整列**一次读完，取读到的 `[N]` 的中位数。

    这是 `game.ranking_nav.RankingNavigator.spin_blind` 闭环注入的 `read_position`。

    ## ⚠️ 为什么不复用 `rows_from_image`

    2026-08-22 实机：闭环拿 `read_rows()` 的行取名次中位数，请求 500 行时
    **第一轮拨完就「读不出名次」，闭环当场失效**。根子是 `rows_from_image`
    按行网格逐行裁剪（`ROW_FIRST_Y + k×ROW_PITCH_PX`），而**滚轮会把列表停在
    非整行位置**（实测偏离网格约 12px）——每一格裁出来的都横跨两行，名次和名字
    全糊。这条限制在设计里本来就写着（`game.ranking_ui` 的滚轮那一段：
    「检测段不许换滚轮」就是这个理由），闭环踩的是同一颗雷。

    整列一次读**与行对齐无关**：不按行切，列表停在哪儿都一样读得出来。
    用户口径（2026-08-22）：「给盲滚单独做一个与行对齐无关的位置读数器」。

    ## ⚠️ 为什么只读名次列

    盲滚只需要回答「在第几名」，不需要名字和分数。名字那列的 OCR 错误率约 8%
    （实机），读它只是徒增一份失败的机会——而这里读失败的代价是闭环退回开环，
    也就是这次改动要治的病本身。

    ## 判据

    - 正则要 `[N]` **成对的方括号**，不是裸数字：整列一次读会把别的东西也吃进来
      （背景文字、被放宽的边界扫到的一点分数列），而那些都不带方括号。
      `\\d{1,4}` 是因为榜单最长是四位名次（`[4781]` 那种五位以上的读数是串出来的）。
    - 取**中位数**不取最大/最小（同 `progress_mark` 的整段实测）：名次列会串出
      高位噪声，当场读到过 `[4781]`，而那一屏真实名次只到 20 左右。
      混进来的还有**自己那一行**（吸附的，名次和当前位置无关）。中位数对两侧
      离群都免疫，前提是样本够多——所以少于 `SPIN_MARK_MIN_ROWS` 个就交 `None`。
    - `median_low` 而不是 `median`：偶数个样本时后者给的是两数的平均，
      也就是一个**榜上不存在的半个名次**。半行的精度在这里毫无意义
      （容差是 `SPIN_TOLERANCE_ROWS` = 8 行），而 `int` 的返回类型是有意义的。

    ⚠️ **读不出就是读不出，这里不重读**（不像 `_rows_confirming` 那样重试）：
    读不出的正常含义是「这一屏本来就没几个 `[N]`」，重读同一帧也一样，
    白等 `READ_ATTEMPTS × REREAD_WAIT_S` = 1.8 秒。真离页了也不该由这里发现——
    盲滚段本来就不读内容，紧接着的检测段第一屏就会认出来。
    """
    columns = columns or RankingColumns()
    strip = image.crop(
        (
            columns.rank[0] - RANK_STRIP_PAD_PX,
            ROW_FIRST_Y - RANK_STRIP_TOP_PAD_PX,
            columns.rank[1] + RANK_STRIP_PAD_PX,
            round(ROW_FIRST_Y + ROWS_PER_SCREEN * ROW_PITCH_PX),
        )
    )
    # 配方复用 `_read_cell(single_line=False)`：灰度、3× LANCZOS、`--psm 6`、`eng`
    # ——和 `name_column_text` 读整条名字列的是同一套。另写一套迟早分家，
    # 而分家那天两边都「读出了数」，只是其中一边读得更差。
    raw = _read_cell(strip, ocr, single_line=False)
    found = [int(number) for number in re.findall(r"\[(\d{1,4})\]", raw)]
    if len(found) < SPIN_MARK_MIN_ROWS:
        return None
    return statistics.median_low(found)


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
    """先**量出**每一行在哪，再逐格 OCR；**名字读不出来**的一行才丢掉。

    ⚠️ **判据是名字，不是名次。** 原先这里是「名次或名字缺一就丢」，
    而 2026-08-14 实机第一屏就打脸：**榜首前三名没有名次数字，是奖章图标**，
    于是最强的三行会被整个扔掉。名次是校验和（`repair_ranks` 能从邻居补），
    名字才是这一层唯一的产物——它反解出坐标，决定舰队飞去哪。

    ⚠️ **自己那一行要按名字剔掉**（见 `is_self_row`）——它是吸附的，
    `RANKING_LIST_MAX_Y` 只挡得住它贴底那一档。

    ## ⚠️⚠️ 行位置是**量出来的**，不是按网格算的（2026-08-23 改）

    原先这里按 `ROW_FIRST_Y + k × ROW_PITCH_PX` 算第 k 行的中心。
    15 屏实机语料证明**网格原点自己在漂**（见 `_row_bands`）：每屏 -7.5px、
    以行距 44.8px 为模回绕、周期 6 屏。偏移一旦超过约 13px，±`ROW_CROP_HALF_HEIGHT`
    的裁剪窗口就把字切掉一截，`--psm 7` **整屏读不出**——那一屏的 bot 全部
    静默丢弃。实测：**每 6 屏里约 3 屏被整屏丢掉**，语料 15 屏里 7 屏归零。

    改法是先读一次**整条名字列**拿到每个词的坐标，聚类出行位置（`locate_rows`），
    再拿**实测中心**去裁剪。取值仍旧是三列逐格 `--psm 7`——整列读只当尺子，
    一个字都不采信（理由写在 `locate_rows` 上，那条界守着「舰队飞去哪」）。

    代价是每屏多一次 OCR（40 次 vs 39 次），换回来两件事：

    1. **不再整屏漏采**——语料 15/15 屏都读满，落库 bot 行 71 → 157（**2.21×**）；
    2. **逐格读本身更准**——偏移 8.5px 那一屏原先把邻行的数字碎片裁进来，
       读出 `10.259K` / `5.954K` / `93.87K` / `3.835K` 这种多一位的值。

    ⚠️ **每屏的绝对耗时基本没变**，快的是「每个 bot 多少秒」：同样约 4 秒一屏，
    原先平均采到 4.7 个 bot，现在 10–12 个。屏数要再降得靠滚轮推进那一步
    （`docs/军力榜采集提速-方案.md` 步 2），跟这里无关。

    ⚠️ **裁剪半高比行距的一半窄。** 星球地表的 `TOTAL CREWS` / `COMMAND OFFICERS`
    透过半透明面板落在 x 769–949（正压在名字列上），y 恰好在两行之间：
    真实行 525，背景在 500 和 548。按 `ROW_PITCH_PX / 2` = 22.4 裁会把上下背景
    各吃进去一点，所以用 `ROW_CROP_HALF_HEIGHT`。
    """
    columns = columns or RankingColumns()
    boxes = [
        (round(center - ROW_CROP_HALF_HEIGHT), round(center + ROW_CROP_HALF_HEIGHT))
        for center in locate_rows(image, ocr, columns)
    ]

    def column_cells(column: tuple[int, int], chosen: list[int]) -> list[Any]:
        return [image.crop((column[0], boxes[i][0], column[1], boxes[i][1])) for i in chosen]

    everything = list(range(len(boxes)))
    names = _read_cells(column_cells(columns.name, everything), ocr)
    # ⚠️ **名次和军力只读「名字读得出」的那些行**，和原先逐格那版一个口径：
    # 名字读不出的一行整行都要丢，那两格读了也是白读。
    kept = [i for i, name in enumerate(names) if name and not is_self_row(name, player_name)]
    ranks = _read_cells(column_cells(columns.rank, kept), ocr)
    scores = _read_cells(column_cells(columns.score, kept), ocr)

    rows: list[RankingRow] = []
    for slot, index in enumerate(kept):
        name = names[index]
        rows.append(
            RankingRow(
                rank=_rank_of(ranks[slot]),
                name=name,
                score=parse_score(scores[slot]),
                coordinate=coordinate_of(name),
            )
        )
    return rows


def locate_rows(image: Any, ocr: Any, columns: RankingColumns | None = None) -> list[float]:
    """读一次整条名字列，返回**实测的行中心**（升序）。

    ## ⚠️⚠️ 整列读在这里是**尺子**，不是读数的

    它只回答「行在哪」——那是个**几何**问题，一个词里的字认错了不影响它的 y。
    三列的取值一律照旧逐格 `--psm 7` 读，裁在这里量出来的中心上。

    这条界必须守死。2026-08-23 语料实测，整列 `--psm 6` 在**名字**列上会认错数字：

        图上 bot_2_55_9    整列读成 bot_2_55_93    ← 位置 93 越界，被坐标校验挡下（丢行）
        图上 bot_7_306_9   整列读成 bot_7_306_3    ← ⚠️ 3 是合法位置，校验挡不住

    第二条是最坏的一种：`7:306:3` 反解得出、区间校验放过，于是**舰队飞到一个
    错的星球**。名字这一列没有任何事后校验兜得住它——军力列有 `descending_breaks`、
    名次列有 `repair_ranks`，名字列什么都没有，它就是这一层的产物本身。

    反过来逐格读也会错（同屏 `bot_2_9_5` 被逐格读成 `bot_2_39_5`），所以这不是
    「哪个读法更好」，而是**错的代价不对称**：逐格读错一格只坏那一行的值，
    整列读错一个字会坏那一行的**归属**，而当它错的是数字时，错出来的还是个
    合法坐标——没有任何下游判据看得出来。

    ⚠️ **行位置由名字列定**，不由名次列或军力列定：名次列榜首三名是奖章图标、
    军力列可能整格读不出，拿它们定行会少几条带。

    搜索窗口比列表区上下各放宽一个行距：行会漂到网格上方约 22px（见 `_row_bands`），
    按列表区硬裁会把漂上去的第一行切掉。放宽之后会捞进标题那一行，由 `_row_bands`
    剔掉（相位不巧时它会漏一条进来，代价是白裁一格——那一格的名字反解不出坐标，
    在 `is_bot_entry` 那道判据上落地，不会变成目标）。
    """
    columns = columns or RankingColumns()
    top = round(ROW_FIRST_Y - ROW_PITCH_PX)
    bottom = round(RANKING_LIST_MAX_Y + ROW_PITCH_PX)
    return _row_bands(
        _words_with_boxes(
            image.crop((columns.name[0], top, columns.name[1], bottom)), ocr, top_offset=top
        )
    )


#: 整列读时，`image_to_data` 里 `conf` 低于这个数的词直接丢。
#:
#: tesseract 对非文字区域会吐出 `conf = -1` 的行；而半透明面板透上来的背景字
#: （`TOTAL CREWS` 之类）置信度也偏低。取 30 是个宽松的下界——**主要的过滤靠
#: 几何**（见 `_row_bands`），这一道只负责把明显的垃圾扫掉。
COLUMN_WORD_MIN_CONF = 30


def _words_with_boxes(
    strip: Any, ocr: Any, *, top_offset: int, upscale: int = 3
) -> list[tuple[float, int, str]]:
    """整条列读一次，返回 `(原图 y 中心, 原图 x 左沿, 文本)`。

    ⚠️ **要坐标而不只要文本**，这是整列读能成立的全部原因：逐格裁剪同时干了
    「切出这一行」和「把行间透字挡在外面」两件事，而整列读一次会把透字一起读进来。
    拿到每个词的坐标之后，那两件事都能在 OCR **之后**用几何补回来，判据与裁剪等价。
    实测 `image_to_data` 和 `image_to_string` **一样快**（真实截图 0.52 秒 / 3 列），
    所以坐标是白拿的。

    走 TSV 而不是 `Output.DICT`：TSV 是纯文本，假的 `ocr` 在测试里只要返回一段
    字符串就能驱动整条路径，不必装 pandas、也不必模仿 pytesseract 的对象。
    """
    from PIL import Image

    grey = strip.convert("L").resize(
        (strip.width * upscale, strip.height * upscale), Image.Resampling.LANCZOS
    )
    tsv = str(ocr.image_to_data(grey, lang="eng", config="--psm 6"))
    words: list[tuple[float, int, str]] = []
    for line in tsv.splitlines()[1:]:  # 第一行是表头
        parts = line.split("	")
        if len(parts) < 12:
            continue
        try:
            left, top, _width, height = (int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9]))
            conf = float(parts[10])
        except ValueError:
            continue
        text = parts[11].strip()
        if not text or conf < COLUMN_WORD_MIN_CONF:
            continue
        # 放大过的坐标要缩回去，再加上裁剪时的偏移，才是原图坐标。
        words.append(((top + height / 2) / upscale + top_offset, round(left / upscale), text))
    return words


def _row_bands(words: list[tuple[float, int, str]]) -> list[float]:
    """把词的 y 中心**聚类**成一行一行，返回每行的实测中心（升序）。

    ⚠️⚠️ **这是整件事的要害：行不落在固定网格上。**

    原先 `rows_from_image` 假设第 k 行的中心是 `ROW_FIRST_Y + k × ROW_PITCH_PX`。
    2026-08-23 的 15 屏实机语料证明**网格原点自己在漂**——每屏 bot 名字相对
    网格的中位偏移：

        -6.1 → -13.7 → -21.1 → +16.1 → +8.6 → +1.1 → -6.4 → -13.8 → -21.4 → ...
        每屏 -7.5px，以行距 44.8px 为模回绕，周期 6 屏

    成因：一次慢拖推进的不是整数行（实测约 8.17 行），那 0.17 行 ≈ 7.5px 的零头
    逐屏累积。偏移一旦超过约 13px，±`ROW_CROP_HALF_HEIGHT`(15) 的裁剪窗口就把字
    切掉一截，`--psm 7` **整屏读不出**——那一屏的 bot 全部静默丢弃。

    实测后果：**每 6 屏里约 3 屏被整屏丢掉**，语料 15 屏里 7 屏归零。生产日志里
    「12, 8, 6 → 0, 0, 0」那个周期 6 的形状就是它，**不是**「榜单真人与 bot 交错」
    ——那个结论是拿同一个坏读法得出来的，等于用 bug 证明 bug。

    两步：先按行距的一半聚类，再用**网格一致性**剔掉不属于列表的带。
    第二步是必要的：搜索窗口为了容纳漂上去的行而放宽了一个行距，会捞进标题
    那一行（实测 y≈236，与列表行差 1.5 个行距而不是整数个）。
    """
    if not words:
        return []
    ys = sorted(y for y, _x, _t in words)
    clusters: list[list[float]] = [[ys[0]]]
    for y in ys[1:]:
        # ⚠️ **判据是「这一组的跨度」，不是「和上一个词的间距」。**
        #
        # 单链聚类（只看相邻间距）在这里会串联：标题那一行是**固定的 UI 元素**
        # （实测 y≈235.7），而列表行的相位在漂，两者的距离恒等于相位差的回绕值
        # ——按定义不超过半个行距，也就是不超过单链的阈值本身。于是「标题单独
        # 成一条带」和「标题被网格判据剔掉」这两件事互斥，某些相位下标题会和
        # 第一条列表行并成一条带，把那条带的中心拽偏约 10px。
        #
        # 按跨度成组就没这个问题：**同一行的词共享基线**，实测跨度最大 1.3px，
        # 而表头离最近那条列表行最少 17.2px。上界走 `ROW_WORD_SPREAD_PX`(6)
        # ——它是按这两个实测值定的，两头都留了几倍余量，账写在那个常量上。
        if y - clusters[-1][0] <= ROW_WORD_SPREAD_PX:
            clusters[-1].append(y)
        else:
            clusters.append([y])
    centers = [sum(c) / len(c) for c in clusters]

    # 网格一致性：列表内的行**彼此**间隔整数个行距，所以 `(y - 基准) mod 行距`
    # 对它们是同一个数（实测同屏内散布 < 1px）。取中位数当基准，偏离超过容差的
    # 剔掉。这剔的是标题行那种「不在行网格上」的东西，不是「偏移大的屏」
    # ——整屏一起漂不影响这个判据，它只看行与行的**相对**位置。
    offsets = [_wrap_offset(c) for c in centers]
    base = statistics.median_low(offsets)
    return [
        c
        for c, off in zip(centers, offsets, strict=True)
        if abs(_wrap_delta(off - base)) <= ROW_GRID_TOLERANCE_PX
        # 上界沿用列表区那条线，放宽半个裁剪窗口以容纳整屏下漂。
        # 再往下就是吸附的自己那一行，而 `is_self_row` 才是它的正主。
        and c <= RANKING_LIST_MAX_Y + ROW_CROP_HALF_HEIGHT
    ]


def _wrap_offset(center: float) -> float:
    """这一行相对行网格的偏移，折进 `[-行距/2, +行距/2)`。"""
    return _wrap_delta(center - ROW_FIRST_Y)


def _wrap_delta(delta: float) -> float:
    half = ROW_PITCH_PX / 2
    return (delta + half) % ROW_PITCH_PX - half


def coordinates_of(rows: Sequence[RankingRow]) -> set[Coordinate]:
    """这一屏**反解出坐标**的那些行的坐标。给重叠自查当尺子。

    ⚠️ **只收非空坐标。** 名字读错的行大多解不出合法坐标（`coordinate_of` 那道
    区间硬闸），于是它们只是不参与比较——而不是像原先那道名次判据一样，
    拿一个读错的数去减出一个假的「漏掉 N 名」。整段账在
    `domain.ranking.screens_overlap` 上。

    ⚠️ 交**集合**而不是列表：判据只问「有没有共同的」，而一屏里同一个坐标
    出现两次（上下两行名字读成同一个）不该影响这个问题的答案。
    """
    return {row.coordinate for row in rows if row.coordinate is not None}


def screen_scores(rows: Sequence[RankingRow], *, anchor: float | None) -> list[float | None]:
    """这一屏哪些军力读数可信——渲染得出来、不破坏降序、也没跌掉一个数量级。

    两道判据叠在一起，顺序要紧：先按「游戏渲染得出来吗」把多插一位的挑掉
    （`renderable_score`），**再**拿它们去和锚点比。反过来的话，一个渲染不出来的
    大数会先当上锚点，把它后面一整段好读数都判成「破坏降序」。
    """
    return trusted_scores(
        [row.score if renderable_score(row.score) else None for row in rows], anchor=anchor
    )


def next_score_anchor(rows: Sequence[RankingRow], *, anchor: float | None) -> float | None:
    """交给**下一屏**的锚点：这一屏可信值里的**最大值**；一个都没有就沿用旧锚点。

    ## ⚠️⚠️ 取最大值，不是取末行

    第一版取的是「最后一个可信值」，那是错的：**相邻两屏必然重叠**（一次拖动推进
    约 8 行，而一屏可见 9–14 行），所以本屏头几行就是上屏的中段，它们的军力
    **理应高于上屏末行**。拿末行当降序基准，每屏开头那 4–5 行会被整段判成
    「破坏降序」：

        上屏  … 10690 10660 10640 10620      末行 10620 当锚点
        本屏  10690 10660 10640 10620 10600 10580
        判据  ✗     ✗     ✗     ✓     ✓     ✓      ← 前三行全被丢

    取上屏的**最大值**（= 它第一行的分数）就对了：那是「下一屏合法可见的最高分」
    ——榜单降序，往下滚只可能看到更小或相等的值。而 93,670 那类偏大 10 倍的
    读数照样挡得住（上屏最大约 9,700，93,670 远超它）。

    ⚠️ 后果本来只是日志噪声（被丢的那几行早在上一屏就以真值入过库，
    `take_batch_targets` 按坐标去重不会写回估算值），但那一行「军力值不可信」
    几乎每屏都会打一次——**而这道判据存在的意义就是让那句话有信号**。
    每屏都喊等于没喊。

    ⚠️ **沿用而不是清空。** 整屏读废（或整屏都被判为不可信）是常态之一，
    清成 `None` 就等于把下一屏也放行——而那正是这道判据要挡的情形。

    ⚠️ **只取正值。** 0 不许当锚点：它当上锚点之后此后每一屏都会被全丢，
    而且不报错。整段账在 `domain.ranking.trusted_scores` 上。
    """
    trusted = [value for value in screen_scores(rows, anchor=anchor) if value]
    return max(trusted) if trusted else anchor


def targets_from_rows(
    rows: list[RankingRow], *, observed_at: datetime, anchor: float | None = None
) -> list[RankingTarget]:
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

    ⚠️ **降序那道网只挡一个方向**，所以前面还有一道 `renderable_score`：
    降序判据认得出「比上一行大」，认不出「这个数游戏根本渲染不出来」。
    2026-08-23 语料实测，15 屏落库的军力值里有 8.3% 是 `10259` / `9935` / `3835`
    这种多插了一位的读数，而它们**比上一行小**，降序判据一个都没抓到。
    """
    repaired = repair_ranks([row.rank for row in rows])
    read = [row.score for row in rows]
    trusted = screen_scores(rows, anchor=anchor)
    dropped = [
        (index, read[index])
        for index, score in enumerate(trusted)
        if score is None and read[index] is not None
    ]
    if dropped:
        # ⚠️ **把锚点和被丢的值一起打出来。**
        #
        # 原先这一句只说「破坏降序」加一串下标，而现在有三条拒收理由
        # （比前一行大 / 比基准大一个数量级 / 比基准小一个数量级），事后分不清是
        # 「读数真错了」还是「锚点本身错了、把好读数误伤了」——那两件事的处置完全
        # 相反。带上锚点和原值就能一眼判：值和锚点差 10 倍是前者，差不到 2 倍是后者。
        say(
            f"军力值不可信，丢掉这几行的分数（坐标保留）"
            f"[锚点 {anchor}]: {[(i, v) for i, v in dropped]}"
        )
    filled = interpolate_scores(trusted)
    targets: list[RankingTarget] = []
    for index, row in enumerate(rows):
        coordinate = row.coordinate
        # ⚠️ **判据吃的是 `row.score`（OCR 的原始读数），不是 `filled[index]`。**
        # 用户口径（2026-08-22）：判 bot 要「id 符合 + 军力不等于 0」，因为
        # `bot_` 前缀是玩家可以改名伪装的，而伪装的真人军力常年是 0。
        # 而上面那条流水线正好会把这个信号擦掉：0 分行一旦被读成空、或者被读成
        # 个大数而撞上降序判据，分数就先丢成 None、再被 `interpolate_scores`
        # 补成两个非零邻居的中点——**插出来的值必然非零**，于是那一行看起来
        # 只是「一个普通的低分 bot」。整段账写在 `domain.ranking.is_bot_entry` 上。
        #
        # `coordinate is None` 那半句是给类型检查器看的：`is_bot_entry` 里面
        # 已经挡掉了它（它复用 `is_bot_coordinate`），但它返回的是 `bool` 而不是
        # `TypeGuard`，narrow 不下来。判据本身不靠这半句。
        if coordinate is None or not is_bot_entry(coordinate, row.score):
            continue
        targets.append(
            RankingTarget(
                coordinate=coordinate,
                military_score=filled[index],
                military_score_at_utc=observed_at,
                # **读到了但被降序判据丢掉**的那些，补回来的值同样是估算——
                # 判据看的是 `trusted` 不是 `read`，否则被丢掉的行会伪装成实读。
                military_score_estimated=trusted[index] is None and filled[index] is not None,
                military_rank=repaired[index],
            )
        )
    return targets


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


def report_bot_area_reached(rows: int, *, blind_rows: int) -> None:
    """记下这一趟实测走了多少**行**才到 bot 区，并在余量被吃掉时喊一声。

    ⚠️ **两件事刻意绑在同一个出口上。** 那句话是自动标定唯一的**样本**
    （`domain.ranking.bot_area_rows` 从 `system_log` 里反解它），而告警是这份
    样本唯一能暴露「盲滚是不是已经滚过头」的时刻。摆成两个各自独立的调用点，
    删掉其中任何一个都不会有东西报错。

    ⚠️ **告警补的是自动标定唯一的盲点。** 标定看不出自己滚过头了：滚过头的表现是
    「第一屏检测就看到 bot」，而那和「刚好停在 bot 起点上」在数据上一模一样——
    两种都记成 `rows == blind_rows`。真滚过头时，被跳过去的那一批 bot
    不会报错、不会少一条日志，只是**采回来的数静悄悄少一截**。

    ⚠️ **单位是行，不再是屏**（2026-08-22 改口径）。滚轮那一段根本没有「屏」这个
    概念，而按行比余量就少一次 `ROWS_PER_SCROLL` 换算——少一次换算就少一处能
    悄悄错量纲的地方。余量还剩 `rows - blind_rows` 行，低于
    `BLIND_SCROLL_MARGIN_ROWS` 就报。

    ⚠️ **不拿 `FIRST_BOT_RANK`(587) 当边界。** 用户口径（2026-08-22）：那段
    「bot 起点」是**玩家改名伪装**出来的，不是真 bot（判据只看名字前缀，改名的
    真人一样命中），真 bot 区在更后面。拿一个被伪装污染的边界报警比不报更坏——
    它会天天喊，而天天喊的告警等于没有告警。这里比的只有两个实测量：本趟走了
    多少行到达 bot 区，和这趟盲滚了多少行。
    """
    say(bot_area_reached_rows_message(rows))
    slack = rows - blind_rows
    if slack >= BLIND_SCROLL_MARGIN_ROWS:
        return
    warn(
        f"⚠️ 盲滚余量告急：本趟实测 {rows} 行到达 bot 区，而盲滚了 {blind_rows} 行，"
        f"余量只剩 {slack} 行（应有 {BLIND_SCROLL_MARGIN_ROWS} 行）。"
        "再漂一点盲滚就会滚过 bot 起点，把榜首那批军力最高的 bot 整段跳过去，"
        "而采回来的数只会静悄悄少一截。请检查攻击配置页上的盲滚行数是不是填得太大。"
    )


# -- 盲滚段：一次连拨，并把这一趟的账记进库 ------------------------------------


@dataclass
class BlindSpinAccount:
    """一趟盲滚的账。**可变**，因为它要跨两个时刻才凑得齐。

    ⚠️ `rows_to_bot_area` 不在这里面：那个数要等检测段翻完才知道，而这份账在
    滚轮刚拨完就有了。两个时刻凑一条日志，所以账先攒着，落库那一步交给
    `report_blind_spin` 在检测段结束之后做。
    """

    #: 要求走多少行（`spin` 收到的那个数）。
    rows_requested: int = 0
    #: 实际发出去多少格。**格数才是真发生的事**，行数是它乘标定算出来的。
    notches_sent: int = 0
    #: 拨完这些格实际用了多久。记它是为了让「每格 16ms」这条可验证：
    #: Windows 上 `time.sleep` 的粒度是 15.6ms，真被撑成 31ms/格的话动量就攒不
    #: 起来，而症状同样是「拨了但没走」。
    spin_seconds: float = 0.0
    #: 拨完之后为等滑行停下来等了多久。**每轮一次**，所以是轮数 × `GLIDE_SETTLE_S`。
    glide_seconds: float = 0.0
    #: 闭环**实测**这一趟走了多少行；开环退路（起点读不出来）时是 None。
    #: ⚠️ None 的含义是「这一趟没测」，不是「没走」——日志上必须分得开。
    rows_measured: int | None = None
    #: 拨了几轮。1 = 一轮就到位（或开环退路）；顶到 `MAX_SPIN_ROUNDS` 说明这一趟
    #: 没收敛，而没收敛是**静默**的（只是少走一截），所以得记下来。
    rounds: int = 0
    #: 每轮观测到的行/格。留整串而不是只留平均：这一族数的**散布**才是要害
    #: （2026-08-22 实测 0.49–1.25），压成一个平均数正好把它抹掉。
    rates: tuple[float, ...] = ()

    @property
    def rows_per_notch_observed(self) -> float | None:
        """这一趟实测每格走了多少行（**总行数 ÷ 总格数**）；测不出就是 None。

        ⚠️ **这是整条盲滚日志的要害。** `ROWS_PER_NOTCH`(1.08) 2026-08-22 被实机
        证伪了：10 个样本落在 0.49–1.25，中位 0.96。闭环之后它不再决定这一趟走多远
        （走多远是量出来的），但「这个数还值不值得当第一轮的猜测」仍旧只有靠每趟
        记实测才答得出。

        ⚠️ **分子是闭环量出来的行数，不是「拨完停在第几名」。** 改闭环之前这里填的
        是绝对名次（`progress_mark`），只因为盲滚总是从榜首起步才碰巧接近行数；
        补拨之后起点不再是 0，那种巧合不成立了。
        """
        if not self.notches_sent or self.rows_measured is None:
            return None
        return round(self.rows_measured / self.notches_sent, 3)


@dataclass(frozen=True, slots=True)
class BlindWalk:
    """盲滚那一段走完之后的账：走了多少行，**以及这个数是量出来的还是算出来的**。

    ⚠️ **两者必须分得开，这就是这个类存在的全部理由。** 原先这一段返回一个裸的
    `int`，而日志照着它打「盲滚 700 行（实走约 700 行）」——那个「实走约」是拿
    格数乘标定算出来的，读起来却像证据。2026-08-22 实机量到真实速率在 0.49–1.25
    之间抽，也就是说那句「实走约 700 行」可能对应 320 行，也可能对应 810 行。
    """

    #: 记账用的行数：闭环时是实测值，开环退路时是按标定折合出来的。
    rows: int
    #: 上面那个数是**量出来的**吗。
    measured: bool


def spin_blind_rows(
    rows: int,
    *,
    spin: Callable[[int], SpinResult],
    account: BlindSpinAccount | None = None,
) -> BlindWalk:
    """连拨滚轮走过 `rows` 行（闭环），记账，返回**走过的行数 + 它是不是量出来的**。

    这就是喂给 `scroll_through_humans` 的那个 `spin`。单拎出来是为了让「记了哪些
    账」测得到：真的那条路要驱动鼠标，单元测试进不去，而这条日志正是这次改动
    唯一能事后自证的东西。

    ⚠️ **量出来的那个数优先，绝不拿格数换算去冒充它。** `game.ranking_nav.spin_blind`
    每一轮都要读一次名次才知道要不要补拨，所以「走了多少行」在那一层就是测量值；
    这里照抄，不再自己另测一遍——那是另一帧画面，两帧之间列表可能已经动过。
    只有**开环退路**（起点名次读不出来）那一支没有测量值，那时才退回
    `实发格数 × ROWS_PER_NOTCH`，并把 `measured=False` 一路带出去让日志说实话。

    ⚠️ 换算值也**不是**传进来的 `rows`：行 → 格那一步要取整，取整之后就已经不是
    原来那个行数了。把请求值当成走过的距离记账，误差会一路带到「实测多少行到达
    bot 区」上，而那正是自标定的输入。
    """
    result = spin(rows)
    if account is not None:
        account.rows_requested = result.rows_requested
        account.notches_sent = result.notches
        account.spin_seconds = round(result.spin_seconds, 3)
        # `spin_blind` **每一轮**拨完等一次 `GLIDE_SETTLE_S`；一格都没拨就一次都不等。
        account.glide_seconds = round(GLIDE_SETTLE_S * result.rounds, 3) if result.notches else 0.0
        account.rows_measured = result.rows_measured
        account.rounds = result.rounds
        account.rates = tuple(round(rate, 3) for rate in result.rates)
    if result.rows_measured is not None:
        return BlindWalk(rows=result.rows_measured, measured=True)
    return BlindWalk(rows=round(result.notches * ROWS_PER_NOTCH), measured=False)


def drag_blind_rows(
    rows: int, *, scroll_blind: Callable[[], None], say_line: Callable[[str], None] = say
) -> BlindWalk:
    """用**慢拖**走过 `rows` 行——盲滚改滚轮之前那条老路，留着当一键回滚。

    ⚠️ **它存在的唯一理由是回滚**，而回滚的开关在**命令行**上：只给
    `--blind-scrolls` 不给 `--blind-rows`，`scan()` 里的 `blind_rows` 就是 `None`，
    整段走到这里来。没有这条路，回滚就变成「改代码 + 发版」。

    ⚠️ **不要把这条路记成「配置里那一列置空」**：
    `storage.models.MilitaryAttackConfigRow.blind_scroll_rows` 置空的含义是
    「跟着代码默认值 700 走」，走的**仍是滚轮**。两种说法一度并存过，
    而它们在配置层不可能同时成立。

    行 → 屏按 `ROWS_PER_SCROLL` 换算。返回的同样是**折合走过的行数**而不是请求值，
    理由同 `spin_blind_rows`：换算取整之后就不是原来那个行数了。

    ⚠️ `measured=False`：这条路一屏都不读，走了多少行全是乘出来的
    （`ROWS_PER_SCROLL` 8.3 自己也是个标定值，生产日志反推是 7.2–7.8）。
    闭环那套「拨完读一次」只在滚轮那条路上；老路就是老路，别让日志把它说成实测。
    """
    screens = round(rows / ROWS_PER_SCROLL)
    say_line(f"盲滚走的是慢拖那条老路：{rows} 行折合 {screens} 屏（滚轮盲滚已关掉）")
    for _screen in range(screens):
        scroll_blind()
    return BlindWalk(rows=round(screens * ROWS_PER_SCROLL), measured=False)


def blind_spin_payload(
    account: BlindSpinAccount, *, rows_to_bot_area: int | None, source: str
) -> dict[str, Any]:
    """那一条盲滚日志的 `payload_json`。

    ⚠️ **`rows_requested` 与 `notches_sent` 都要留**，别只留一个：格数是真发生
    的事，行数是它乘标定算出来的，而这条日志存在的意义就是让「标定还成不成立」
    在事后答得出来——只留折合值就把两者的差别抹平了。

    `rows_per_notch_calibrated` 把**当时代码里的标定值**一起存下来：日后有人把
    1.08 改了，库里这一批老记录才说得清它们是按哪个数算的。

    `rounds` / `rows_per_notch_by_round` 是闭环带来的两把尺子：前者答「这一趟补拨
    了几轮、有没有顶到 `MAX_SPIN_ROUNDS` 上限」（顶到了就是没收敛，而没收敛是静默
    的），后者答「同一趟里速率抖得有多厉害」——那正是 2026-08-22 推翻开环的那份
    证据（0.49–1.25）该继续被盯住的地方。`rounds == 1` 且 `rows_measured is None`
    的那种记录就是**开环退路**：起点名次读不出来，这一趟没有闭环保护。
    """
    return {
        "rows_requested": account.rows_requested,
        "notches_sent": account.notches_sent,
        "spin_seconds": account.spin_seconds,
        "glide_seconds": account.glide_seconds,
        "rows_measured": account.rows_measured,
        "rows_per_notch_observed": account.rows_per_notch_observed,
        "rows_per_notch_calibrated": ROWS_PER_NOTCH,
        "rounds": account.rounds,
        "rows_per_notch_by_round": list(account.rates),
        "rows_to_bot_area": rows_to_bot_area,
        "source": source,
    }


def report_blind_spin(
    account: BlindSpinAccount,
    *,
    rows_to_bot_area: int | None,
    source: str,
    record: Callable[[str, dict[str, Any]], None],
) -> None:
    """把这一趟盲滚落 `system_log`（**落库不落文件**）。

    ⚠️ **落库而不是打文件。** 实机在另一台机器上，本地 `var/logs` 是陈旧的；
    「这个标定还成不成立」这个问题要在控制台的日志页上答得出来。

    正文里那句「每格实测 N 行」是给人看的，机器读的是 `payload_json`——
    别把它当成解析入口（那种双身份的正文只有「翻了 N 行到达 bot 区」一处，
    而那一处的措辞代价写在 `domain.ranking.bot_area_reached_rows_message` 上）。
    """
    observed = account.rows_per_notch_observed
    walked = (
        f"实测走了 {account.rows_measured} 行"
        if account.rows_measured is not None
        else "实走多少行没测出来（起点名次读不出，这一趟是开环、没有闭环保护）"
    )
    tail = (
        f"，每格实测 {observed} 行（第一轮的猜测是 {ROWS_PER_NOTCH}）"
        if observed is not None
        else ""
    )
    record(
        f"盲滚请求 {account.rows_requested} 行：{walked}；"
        f"补拨 {account.rounds} 轮共发了 {account.notches_sent} 格、"
        f"拨完用 {account.spin_seconds:.1f} 秒{tail}",
        blind_spin_payload(account, rows_to_bot_area=rows_to_bot_area, source=source),
    )


# -- 翻真人段：留现场、确认式判空 ----------------------------------------------


class ScanStage(Enum):
    """本趟走到了哪一段。**收尾那句话靠它区分「被掐」与「跑满」**（见 `completion_message`）。"""

    BLIND = "盲滚中"
    DETECTING = "检测中"
    COLLECTING = "采集中"
    CLOSED = "已收尾"


@dataclass
class ScanProgress:
    """一趟采集的进度。可变，因为它要在 `finally` 里被读到——包括 Ctrl+C 那一次。"""

    stage: ScanStage = ScanStage.BLIND
    #: 这一趟盲滚了多少**行**（进入检测段之前）。
    blind_rows: int = 0
    #: 真人段总共走了多少**行**，**含盲滚那一段**。
    human_rows: int = 0
    #: 检测段翻了几屏。⚠️ **这一项的单位仍旧是屏**，因为检测段照旧慢拖
    #: （滚轮会把列表停在非整行位置，逐行裁剪就横跨两行、名字全糊）。
    detection_scrolls: int = 0
    #: 采集段滚了几屏。
    collect_scrolls: int = 0


@dataclass(frozen=True)
class NameSample:
    """翻真人段途中的一次抽样。

    ⚠️ **这就是 2026-08-18 那一轮缺的东西。** 那趟「采不到」里，
    「列表根本没动」「开错了面板」「bot 名字读不出来」三种可能完全不可分辨——
    循环一个字现场都没留。三个字段各自否掉一种：`excerpt` 说明读到了什么
    （空 = 没在榜单页上 / 读不出），`overlap` 说明列表动没动。
    """

    #: 抽样发生在**检测段**第几屏。盲滚那一段不计在内：它一次连拨，中间没有
    #: 「屏」这个刻度，也没有可抽样的时刻。
    scrolled: int
    excerpt: str
    #: 与**上一次抽样**的重合率；第一次抽样没有上一次，是 None。
    overlap: float | None

    @property
    def stuck(self) -> bool:
        """重合率高到说明列表压根没在动。"""
        return self.overlap is not None and self.overlap > NAME_SAMPLE_STUCK_OVERLAP


def name_excerpt(text: str) -> str:
    """把整条名字列压成一行摘要，截到 `NAME_SAMPLE_CHARS`。

    压掉换行与连续空白：进 `payload_json` 的东西要能一眼比对，而 `--psm 6` 的
    输出里换行位置本身就抖。
    """
    return " ".join((text or "").split())[:NAME_SAMPLE_CHARS]


def sample_overlap(previous: str, current: str) -> float:
    """两条名字列摘要有多像（0 = 毫不相干，1 = 一模一样）。

    用 `difflib` 而不是集合交并：名字列跑的是 `eng`，中文玩家名读回来是噪声串，
    同一行连读两次也不会逐字相同——要的是「这一屏和上一屏是不是同一批人」，
    而那是个**序列相似度**问题。标准库、确定性、不引依赖。

    ⚠️ 它只当**诊断**用，不做收工判据。`track_progress` 那段注释里记着四次
    假阳性的账：任何建在 OCR 噪声上的进度判据都别拿来决定停不停。
    """
    if not previous and not current:
        return 1.0
    return difflib.SequenceMatcher(None, previous, current).ratio()


def read_name_column_confirming(
    read: Callable[[], str], wait: Callable[[float], None]
) -> tuple[str, tuple[str, ...]]:
    """读整条名字列，**空结果重读几次再认**。返回 `(读数, 每次各读到什么)`。

    ⚠️ **这里原先是单帧判空，是全仓唯一的漏网处。** `game.ranking_nav` 模块头
    第一条规矩就是「空结果不是证据」，而同一调用栈里所有别的读法都重读 3 次
    （`RankingNavigator._rows_confirming` / `_labels_confirming`、
    `preset_picker.read_names_confirming`、`vision.scan_reading.read_panel_confirming`）。
    只有翻真人段这一处读到空就**当场**判离页、把整趟作废。

    代价是实打实的：全历史三次触发在第 **101 / 79 / 78** 屏，而 bot 区就在
    **78–82**——**两次是差一屏就到，整趟扔了**。

    次数与间隔**复用现成的** `READ_ATTEMPTS` / `REREAD_WAIT_S`，不新开常量：
    它要的就是和那几处同一条规矩，各写一份迟早分家。

    三次各读到什么原样返回，由调用方写进 `payload_json`——「三次都空」和
    「第一次空、后两次读出半屏」是两种故障，日志得分得开。
    """
    seen: list[str] = []
    for attempt in range(READ_ATTEMPTS):
        text = read()
        seen.append(text)
        if text:
            return text, tuple(seen)
        if attempt + 1 < READ_ATTEMPTS:
            wait(REREAD_WAIT_S)
    return "", tuple(seen)


@dataclass(frozen=True)
class HumanStretch:
    """翻真人段的结局。"""

    #: 见到 bot 名字了吗。False = 这一趟到此为止。
    reached_bots: bool
    #: 一共走了多少**行**（盲滚那一段 + 检测段的屏折合过来的行）。
    #: 这就是喂给自标定的那个实测量，见 `report_bot_area_reached`。
    rows: int
    #: 检测段翻了几屏。**盲滚那一段不计在内**——它没有「屏」这个单位。
    detection_scrolls: int
    #: 为什么停下来的，给人看的一句话。
    reason: str
    samples: tuple[NameSample, ...] = ()


def scroll_through_humans(
    *,
    scroll: Callable[[], None],
    spin: Callable[[int], BlindWalk],
    read_names: Callable[[], str],
    wait: Callable[[float], None],
    blind_rows: int,
    detection_budget: int,
    say_line: Callable[[str], None],
    record: Callable[[str, dict[str, Any]], None] = lambda _m, _p: None,
    progress: ScanProgress | None = None,
) -> HumanStretch:
    """盲滚 + 检测，一直翻到名字列里出现 bot 名字为止。

    ⚠️ **两段用两种动作，别把它们合成一种。**

    - 盲滚段只调**一次** `spin(blind_rows)`：滚轮连拨、末尾统一等一次滑行。
      原先这里是 `for _ in range(blind_scrolls): scroll()`，70 屏 × 每屏等 2 秒
      = 生产实测 294.6 秒，而这次改动的收益**全部**来自把那些等待合并成一次。
      改在这一层而不是 `scroll_blind()` 内部，就是为了不把 70 次等待原样留下。
    - 检测段仍旧一屏一次 `scroll()`（慢拖），**一个字都不许换成滚轮**：滚轮会把
      列表停在非整行位置，而 `rows_from_image` 是按 `ROW_FIRST_Y + k×ROW_PITCH`
      逐行裁剪的——偏了就横跨两行，名字全糊。实测过一次：画面清晰，
      `rows_from_image` 只读出 2 个名次。

    `spin` 吃行数、返回一个 `BlindWalk`：走过多少行，**以及那个数是量出来的还是
    算出来的**。怎么走由调用方决定：`spin_blind_rows` 是滚轮那条路（闭环，常态下
    量得出），`drag_blind_rows` 是留作回滚的慢拖那条路（一屏都不读，只能乘出来）
    ——这一层两条都不认识，它只负责**不把算出来的数说成量出来的**。

    `detection_budget` 是**检测段自己的屏数预算**，与盲滚走多远无关。整段道理写在
    `game.ranking_ui.BOT_DETECTION_BUDGET_SCROLLS` 上：原先那个耦合是隐式且反向的
    （盲拖调大，检测预算等量缩小），换成行口径之后连相减的机会都没有了。

    每 `NAME_SAMPLE_EVERY_SCROLLS` 屏留一次现场（`NameSample`），到顶时把整串
    交出去，好让日志说得出这一趟里名字列**一直在变 / 一直没变 / 最后读到的是什么**。
    """
    progress = progress if progress is not None else ScanProgress()
    progress.stage = ScanStage.BLIND
    progress.blind_rows = blind_rows
    # 0 行是最保守的合法取值（「一格都别拨」）：那时连 `spin` 都不调，
    # 整个真人段退化成纯检测——也就是最保守的那一头。
    walk = spin(blind_rows) if blind_rows else BlindWalk(rows=0, measured=False)
    rows_spun = walk.rows
    progress.human_rows = rows_spun
    progress.stage = ScanStage.DETECTING
    if blind_rows:
        # ⚠️ **「请求多少行」和「实走多少行」必须是两个数，而且要说清后者是不是量的。**
        # 原先这里写的是「盲滚 700 行（实走约 700 行…）」，那个「实走约」是拿格数
        # 乘标定算出来的，读起来却像证据——而 2026-08-22 实机量到真实速率在
        # 0.49–1.25 之间抽，同一句话可能对应 320 行，也可能对应 810 行。
        walked = (
            f"实测走了 {rows_spun} 行"
            if walk.measured
            else f"实走多少行没测出来，按 {rows_spun} 行记账"
        )
        say_line(f"盲滚请求 {blind_rows} 行：{walked}（那一段必定还是真人），开始检测 bot")

    scrolled = 0
    samples: list[NameSample] = []
    marker, attempts = read_name_column_confirming(read_names, wait)

    def rows_now() -> int:
        """到此刻为止走了多少行 = 盲滚的行 + 检测段的屏 × 每屏行数。

        两段的单位不一样，所以账只能在这里合。⚠️ 换算只有这一处：多写一处
        `ROWS_PER_SCROLL` 就多一处能悄悄错量纲的地方，而错了量纲的表现是
        自标定给出一个离谱的数，不是一条报错。
        """
        return rows_spun + round(scrolled * ROWS_PER_SCROLL)

    def take_sample() -> None:
        excerpt = name_excerpt(marker)
        previous = samples[-1].excerpt if samples else None
        sample = NameSample(
            scrolled=scrolled,
            excerpt=excerpt,
            overlap=None if previous is None else round(sample_overlap(previous, excerpt), 3),
        )
        samples.append(sample)
        say_line(
            f"  翻真人段 {scrolled} 屏…名字列 {sample.excerpt!r}"
            + ("" if sample.overlap is None else f"（与上次重合 {sample.overlap:.2f}）")
        )
        record(
            f"翻真人段 {scrolled} 屏：名字列摘要与上一次抽样的重合率",
            {
                "scrolled": scrolled,
                "rows": rows_now(),
                "name_excerpt": sample.excerpt,
                "overlap": sample.overlap,
                "list_looks_stuck": sample.stuck,
            },
        )

    while not mentions_bot(marker):
        if not marker:
            # 三次都读不出来才认：整条名字列一个字都没有 = 已经不在榜单页上。
            say_line(f"检测段第 {scrolled} 屏之后名字列连读 {READ_ATTEMPTS} 次全空；已离页")
            record(
                f"检测段第 {scrolled} 屏之后名字列连读 {READ_ATTEMPTS} 次全空，判为已离页",
                {
                    "scrolled": scrolled,
                    "rows": rows_now(),
                    "reads": list(attempts),
                    "samples": _samples_payload(samples),
                },
            )
            return HumanStretch(
                reached_bots=False,
                rows=rows_now(),
                detection_scrolls=scrolled,
                reason=f"名字列连读 {READ_ATTEMPTS} 次全空，已离页",
                samples=tuple(samples),
            )
        if scrolled >= detection_budget:
            reason = f"盲滚 {blind_rows} 行之后又翻满 {detection_budget} 屏检测预算仍没见到 bot"
            say_line(f"{reason}；本轮到此为止")
            record(
                reason + "；本轮到此为止",
                _budget_payload(samples, scrolled=scrolled, rows=rows_now()),
            )
            _say_sample_verdict(samples, say_line)
            return HumanStretch(
                reached_bots=False,
                rows=rows_now(),
                detection_scrolls=scrolled,
                reason=reason,
                samples=tuple(samples),
            )
        scroll()
        scrolled += 1
        progress.detection_scrolls = scrolled
        progress.human_rows = rows_now()
        marker, attempts = read_name_column_confirming(read_names, wait)
        if scrolled % NAME_SAMPLE_EVERY_SCROLLS == 0:
            take_sample()
    return HumanStretch(
        reached_bots=True,
        rows=rows_now(),
        detection_scrolls=scrolled,
        reason="名字列里出现了 bot",
        samples=tuple(samples),
    )


def _samples_payload(samples: Sequence[NameSample]) -> list[dict[str, Any]]:
    return [
        {"scrolled": s.scrolled, "name_excerpt": s.excerpt, "overlap": s.overlap} for s in samples
    ]


def _budget_payload(samples: Sequence[NameSample], *, scrolled: int, rows: int) -> dict[str, Any]:
    overlaps = [s.overlap for s in samples if s.overlap is not None]
    return {
        "scrolled": scrolled,
        "rows": rows,
        "samples": _samples_payload(samples),
        "last_name_excerpt": samples[-1].excerpt if samples else "",
        "stuck_samples": sum(1 for s in samples if s.stuck),
        "max_overlap": max(overlaps) if overlaps else None,
    }


def _say_sample_verdict(samples: Sequence[NameSample], say_line: Callable[[str], None]) -> None:
    """到顶时不只说「翻满 N 屏」，还要说清这一路上名字列到底动没动。

    ⚠️ **这一句就是那三种可能的分辨器。** 「一直没变」= 列表压根没滚（或者开错了
    面板）；「一直在变」= 滚是滚了，只是没见到 bot（判据或榜单长度出了事）；
    最后读到的那一串则直接回答「读出来的是不是榜单」。
    """
    if not samples:
        say_line("  这一趟一次抽样都没留下（还没翻到第一次抽样点就停了）")
        return
    stuck = sum(1 for sample in samples if sample.stuck)
    if stuck == len(samples) - 1 and len(samples) > 1:
        moved = "名字列**一直没变**：列表压根没在滚（或者根本不在榜单页上）"
    elif stuck:
        moved = f"名字列有 {stuck}/{len(samples) - 1} 次抽样几乎没变"
    else:
        moved = "名字列一直在变：列表确实在滚，只是没见到 bot"
    say_line(f"  {len(samples)} 次抽样：{moved}；最后读到 {samples[-1].excerpt!r}")


def exit_code_for_stretch(stretch: HumanStretch) -> int:
    """真人段这一段该给整趟留个什么退出码。

    ⚠️ **单拎出来是为了让「用哪个码」这件事测得到。** `scan()` 本身要真驱动，
    单元测试进不去；而这个选择恰恰是最容易被悄悄改回去的一处——原先它是 `2`，
    而 2 是 `argparse` 的（整段账在 `domain.scheduler.EXIT_RANKING_INCOMPLETE`）。
    """
    return 0 if stretch.reached_bots else EXIT_RANKING_INCOMPLETE


# -- 收尾那句话 ----------------------------------------------------------------


def completion_message(progress: ScanProgress, *, written: int, suspect: int, outcome: int) -> str:
    """收尾那一句。**必须说清本趟走到了哪一段、走了多少行。**

    ⚠️ 原先它只说「逐屏写入 0 条」，对「跑 2.2 分钟被用户掐」和「跑满预算一无所获」
    说的是同一句话——2026-08-18 排障时**我据此对用户下过错误结论**。两者的善后
    完全相反：前者什么都不用管（本来就没跑到采集段），后者说明判据或版面坏了。

    ⚠️ **两段的单位不一样，句子里就要写不一样。** 盲滚段是行（滚轮没有「屏」这个
    概念），检测段与采集段是屏（照旧慢拖）。把它们凑成一个数会让「盲滚 700」
    看起来像 700 屏，而那是 8.3 倍的量纲差。

    ⚠️ **它在 `finally` 里被调用**，所以 Ctrl+C / 调度器抢占那一路也留得下这一句。
    """
    stage = progress.stage
    verdict = "完成" if outcome == 0 and stage is ScanStage.CLOSED else f"停在「{stage.value}」"
    return (
        f"军事榜采集{verdict}："
        f"真人段走了 {progress.human_rows} 行"
        f"（盲滚 {progress.blind_rows} 行 + 检测 {progress.detection_scrolls} 屏），"
        f"采集段滚了 {progress.collect_scrolls} 屏；"
        f"逐屏写入 {written} 条，其中末屏可疑 {suspect} 条"
    )


def scan(
    columns: RankingColumns | None = None,
    *,
    blind_scrolls: int = BLIND_SCROLLS,
    blind_rows: int | None = BLIND_SCROLL_ROWS,
    blind_rows_source: str = "default",
    detection_budget: int = BOT_DETECTION_BUDGET_SCROLLS,
    bot_scrolls: int = 400,
    bot_limit: int | None = None,
) -> int:
    """跑一趟榜单采集。

    返回 0 = 正常到底，`EXIT_RANKING_INCOMPLETE` = 没走完整趟（中途离页，或者翻满
    检测预算仍没见到 bot 区）。**那个码不是 2**——2 是 `argparse` 的，整段理由写在
    `domain.scheduler.EXIT_RANKING_INCOMPLETE` 上。

    盲滚段走哪条路由 `blind_rows` 决定，**两个旋钮的优先级是显式的**：

    - `blind_rows` 是行数（默认 `BLIND_SCROLL_ROWS`），走滚轮连拨那条路，
      此时 `blind_scrolls` 一概不看。
    - `blind_rows=None` 是**一键回滚**：退回慢拖 `blind_scrolls` 屏那条老路。
      `storage.models.MilitaryAttackConfigRow.blind_scroll_rows` 上写着这条路要
      留着，好让回滚不必改代码、不必重新发版。

    `blind_rows_source` 只进日志（`"cli"` = 命令行给的，`"default"` = 用了代码
    默认值）。**它答不出「手填还是自标定」**——那个区别只有调度器知道，写在它自己
    那条「盲滚行数判定为…」的记录里，两条按时间对起来看。

    ⚠️ **离页也要入库。** 原先这里 `return 2` 排在 `save_ranking_targets` 前面，
    于是断线就把这一趟全扔了——而交接文档写着**断线是预期结果**（2026-08-14
    实机滚到第 473 名就断）。照那个写法，实机上大概率一条都存不下来。

    离页时只丢**最后一屏**：那一屏是在画面已经变了之后读的，可疑；
    它之前那些是画面正常时读到的，和正常到底的那些一样可信。
    """
    import pytesseract

    if bot_limit is not None and bot_limit < 1:
        raise ValueError("bot_limit must be at least 1")
    # 0 合法（盲滚「一格都别拨」、盲拖「一屏都别拖」都是最保守的取值），负数不是。
    if blind_scrolls < 0:
        raise ValueError("blind_scrolls must not be negative")
    if blind_rows is not None and blind_rows < 0:
        raise ValueError("blind_rows must not be negative")
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
        # 「这一行的分数解析出来了没有」——`ranking_nav` 拿它判**面板铺开了没有**
        # （不是判页签，那个判据不存在）。行的形状只有这一层认识，所以由这里注入。
        row_has_score=lambda row: row.score is not None,
        # 「我现在滚到第几名了」——`spin_blind` 的闭环拿它答这一个问题，
        # 拨完读一次、不够就补拨。
        #
        # ⚠️ **刻意不是 `read_rows` 那条路。** `rows_from_image` 逐行裁剪，而滚轮
        # 把列表停在非整行位置，裁出来横跨两行、名次全糊（2026-08-22 实机：请求
        # 500 行，第一轮拨完读不出名次，闭环当场失效）。`position_from_image`
        # 整列一次读完，与行对齐无关——整段账写在它自己的文档串上。
        read_position=lambda: position_from_image(driver.capture(), ocr, columns),
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

    def record_log(message: str, payload: dict[str, Any]) -> None:
        record_system_log("INFO", "tools.ranking_scan", message, payload=payload)

    # 重叠账：`previous_coordinates` 是上一屏读出来的那些坐标，
    # `screens_without_overlap` 是整趟有几屏和上一屏一个坐标都没对上。
    # 见 `domain.ranking.screens_overlap`（那里还记着为什么这道判据**不看名次**）。
    #
    # ⚠️ **提到 `try` 之前**，理由和下面那句收尾一样：被 Ctrl+C / 调度器抢占打断时
    # 也要报得出「这一趟有几屏没接上」。定义在循环里的话，`finally` 会撞 `NameError`
    # ——而那会把一次干净的中断变成一条堆栈。
    previous_coordinates: set[Coordinate] = set()
    screens_without_overlap = 0
    # ⚠️ **军力锚点要跨屏活着。** 上一屏最后一个可信的军力值，用来判下一屏的
    # 第一行——`descending_breaks` 是按屏跑的，屏首那一行原先没有任何约束，
    # 而 2026-08-23 生产实测正是从那里漏进去一个 10 倍偏大的值
    # （账在 `domain.ranking.trusted_scores`）。
    score_anchor: float | None = None
    #: 连着几屏一个军力值都没采信——自愈阀的计数器，账见循环里那段注释。
    blind_score_screens = 0

    account = BlindSpinAccount()
    if blind_rows is None:
        # -- 回滚路径：盲滚段退回慢拖 ---------------------------------------
        # 行 ↔ 屏各换算一次，来回是恒等的（40 屏 × 8.3 = 332 行 → 40 屏）。
        blind_phase_rows = round(blind_scrolls * ROWS_PER_SCROLL)

        def spin(rows: int) -> BlindWalk:
            return drag_blind_rows(rows, scroll_blind=nav.scroll_blind, say_line=say)
    else:
        blind_phase_rows = blind_rows

        def spin(rows: int) -> BlindWalk:
            # ⚠️ **「走了多少行」不在这一层测。** `spin_blind` 每一轮都要读一次名次
            # 才知道要不要补拨，所以那一层手里就有测量值；这里再测一次是另一帧画面，
            # 而两帧之间列表可能已经动过（原先这里挂着一个 `measure_rows`，
            # 记进日志的其实是「拨完停在第几名」，不是「走了多少行」）。
            return spin_blind_rows(
                rows,
                spin=lambda requested: nav.spin_blind(rows=requested),
                account=account,
            )

    # ⚠️ **开榜放在 try 外面。** 它在读标签行那一步就可能失败，那时面板压根没开，
    # 而 `nav.close()` 会点 `RANKING_CLOSE`(750, 71) ——**在认不出的画面上点击**，
    # 那是这条链路的硬红线。放在外面就没有「记得判断开没开」这回事：
    # 抛出去的时候根本走不到 finally。
    nav.open_military_ranking()

    screens: list[list[RankingTarget]] = []
    outcome = 0
    progress = ScanProgress()
    try:
        # -- 第一段：翻真人段，只问「到 bot 区了没有」 ----------------------
        #
        # 用户口径（2026-08-15）：「你应该不停的滚屏，直到你识别到了 bot 关键字，
        # 这样就可以了，然后再开始取军力」。
        #
        # ⚠️ **这一段刻意不判「滚到底了没有」。** 那条判据建在名次 OCR 上，而名次
        # 恰恰是唯一读不准的一列（榜首的 1–2 位数尤其串），实机 2026-08-15 连着
        # 假阳性四次。而 bot 名字是纯 ASCII、读得稳——把判据换到读得准的信号上，
        # 整段就不需要那条判据了。这一段只靠 `detection_budget` 兜底。
        # 头 `blind_phase_rows` 行连检测都省了——那一段**必定**还在真人区。
        #
        # ⚠️ **`scroll` 与 `spin` 是两个不同的动作，别合成一个。** 检测段照旧慢拖
        # （`nav.scroll_blind`）；换成滚轮会把列表停在非整行位置，逐行裁剪就横跨
        # 两行、名字全糊（实测：画面清晰，一屏只读出 2 个名次）。
        stretch = scroll_through_humans(
            scroll=nav.scroll_blind,
            spin=spin,
            read_names=lambda: name_column_text(driver.capture(), ocr, columns),
            wait=driver.wait,
            blind_rows=blind_phase_rows,
            detection_budget=detection_budget,
            say_line=say,
            record=record_log,
            progress=progress,
        )
        outcome = exit_code_for_stretch(stretch)
        if stretch.reached_bots:
            # ⚠️ 这一句不只是给人看的：它是**自动标定唯一的实测样本来源**，
            # 而同一个出口还负责在余量被吃掉时报警。别把它拆回一句 `say`。
            report_bot_area_reached(stretch.rows, blind_rows=blind_phase_rows)
        if account.rows_requested:
            # ⚠️ **落在这里而不是拨完那一刻**，因为 `rows_to_bot_area` 要等检测段
            # 跑完才知道，而那个数与「每格实测几行」放在同一条里才对得上账。
            # 慢拖那条回滚路不会走到这里（`account` 一个字段都没填）——它本来就
            # 没有格数可记。
            report_blind_spin(
                account,
                rows_to_bot_area=stretch.rows if stretch.reached_bots else None,
                source=blind_rows_source,
                record=record_log,
            )

        # -- 第二段：细读三列 ------------------------------------------------
        if outcome == 0:
            progress.stage = ScanStage.COLLECTING
            rows = read_rows()
            first, reached_limit = collect(
                targets_from_rows(rows, observed_at=datetime.now(UTC), anchor=score_anchor)
            )
            score_anchor = next_score_anchor(rows, anchor=score_anchor)
            screens.append(first)
            if reached_limit:
                say(f"已采够军力攻击批次 {bot_limit} 个 bot；交给攻击任务")
            dry = 0
            previous_coordinates = coordinates_of(rows)
            for extra in range(1, 0 if reached_limit else bot_scrolls + 1):
                progress.collect_scrolls = extra
                step = nav.scroll_once()
                if step.outcome is ScrollOutcome.OFF_PAGE:
                    say(f"采集第 {extra} 滚之后离页（多半断线）；丢掉最后一屏")
                    outcome = EXIT_RANKING_INCOMPLETE
                    break
                rows = list(step.rows)
                fresh, reached_limit = collect(
                    targets_from_rows(rows, observed_at=datetime.now(UTC), anchor=score_anchor)
                )
                score_anchor = next_score_anchor(rows, anchor=score_anchor)
                # ⚠️ **自愈阀：连着几屏整屏被判掉，就把锚点撤掉重新起头。**
                #
                # 锚点错了的后果是不对称的：它会把**后面每一屏**的好读数都判成
                # 「破坏降序」，而整屏被判掉又让锚点「沿用」下去——一个错值就能
                # 让整趟余下的军力值全空，且每屏只打一句「丢掉这几行」，没有累计信号。
                # （2026-08-23 实测过一种入口：第一屏中位数为 0，见
                # `domain.ranking.trusted_scores`；那条已经堵了，但堵的是入口，
                # 不是这个形状本身。）
                #
                # 撤掉锚点的代价只是「下一屏按自己的中位数起头」，而收益是把一次
                # 永久性静默失败压成两屏的颠簸。
                if any(target.military_score is not None for target in fresh):
                    blind_score_screens = 0
                else:
                    blind_score_screens += 1
                    if blind_score_screens >= SCORE_ANCHOR_RESET_SCREENS:
                        say(
                            f"⚠️ 连着 {blind_score_screens} 屏一个军力值都没采信"
                            f"（锚点 {score_anchor}）：撤掉锚点重新起头"
                        )
                        record_log(
                            "军力锚点重置",
                            {"screens": blind_score_screens, "anchor": score_anchor},
                        )
                        score_anchor = None
                        blind_score_screens = 0
                # ⚠️ **别在 bot 区的边界上提前收工。** 2026-08-15 实机：刚翻到
                # bot 区时那几屏大半还是真人，本来就没几个新 bot，而
                # `SCROLL_STALL_CONFIRMATIONS`(3) 当场就触发了——一趟只写了 2 条，
                # 而 bot 段有四千多个。
                #
                # bot 段里每屏期望 8 个新的（实测），所以连着 `DRY_SCREENS` 屏
                # 一个都没有才算真的到头。跑不满就由 `bot_scrolls` 预算兜底。
                dry = 0 if fresh else dry + 1
                screens.append(fresh)
                # ⚠️ **重叠断了必须留下痕迹。** 见 `domain.ranking.screens_overlap`：
                # 跳过去的那几行压根没被读过，所以「采到的 bot 数」看起来完全正常
                # ——和 2026-08-23 修掉的那个整屏漏采是同一类静默失败。
                # 这里只观测不拦（名字也会读错，一次没对上就中断整趟不值），
                # 拦不拦等推进量提上去再定。
                #
                # ⚠️ **只说「可能」，也不说「漏了几行」。** 跳过去的行没被读过，
                # 「几行」这个数在原理上就无从得知——原先那道名次判据敢报「漏掉
                # 8922 名」（整趟只走了 570 名），正因为它是从两个带噪声的名次
                # 减出来的。整段账在 `screens_overlap` 上。
                seen_here = coordinates_of(rows)
                overlap = screens_overlap(previous_coordinates, seen_here)
                if overlap is False:
                    screens_without_overlap += 1
                    say("  ⚠️ 与上一屏没有一个共同坐标：重叠可能断了（中间的行没被读过）")
                # ⚠️ **不许写 `or previous_coordinates`。** 这一屏坐标全读不出时，
                # 保留上一屏的集合会让下一屏拿「隔两屏」的坐标去比——而隔两屏本来
                # 就不该有共同坐标（一次拖动推约 8 行，两次就推出一整屏），于是
                # 每一次读废都要连带造出一条**假警报**。而假警报比不报更坏：
                # 它把这条判据教成「经常喊狼来了」，真断的那次就没人看了。
                # 读不出就该是空集，让下一次比较答「不知道」。
                previous_coordinates = seen_here
                # ⚠️ **「读出几行」必须和「本屏 bot 几个」一起记。**
                #
                # 原先只记后者，于是「这一屏没有新 bot」和「这一屏整个没读出来」
                # 在日志里长得一模一样。2026-08-23 那个漏采一半的缺陷就是这么埋住的：
                # 生产日志里「12, 8, 6 → 0, 0, 0」被当成「榜单真人与 bot 交错」，
                # 而真相是那三屏各有 10–12 个 bot、一个都没读出来。要分开这两件事
                # 当时得临时写只读探针——那正是「出事时能只靠库里日志定位」不成立的样子。
                say(
                    f"  采集第{extra:>3}滚 读出 {len(rows):>2} 行 "
                    f"本屏 bot {len(fresh)} 连续空屏 {dry}"
                )
                record_log(
                    "采集一屏",
                    {
                        "scroll": extra,
                        "rows_read": len(rows),
                        "bots_fresh": len(fresh),
                        "dry_screens": dry,
                        # `True` 重叠上了 / `False` 一个坐标都没对上 /
                        # `None` 有一屏坐标全读不出，答「不知道」。
                        # ⚠️ `None` 不许落成 `False`：那会让最可疑的那几屏
                        # （连名字都读不出的）在日志里长得像「重叠断了」。
                        "overlap_intact": overlap,
                    },
                )
                if reached_limit:
                    say(f"已采够军力攻击批次 {bot_limit} 个 bot；交给攻击任务")
                    break
                if dry >= DRY_SCREENS:
                    say(f"连续 {dry} 屏没有新 bot：这一段到头了")
                    break
            if outcome == 0:
                progress.stage = ScanStage.CLOSED
    finally:
        if not nav.close():
            say("排行榜已关闭，但导航条还原未确认")
        # 离页时最后一屏是画面已经变了之后读的，可疑——但它**已经逐屏写进去了**。
        # `keep_screens` 在这里只用来报数，不再决定写什么：真要把它撤回来得删行，
        # 而删行比留一条可疑记录危险得多（那条记录带着 source='ranking'，
        # 本来就标着未验证）。
        #
        # ⚠️ **收尾那句话放在 `finally` 里**，为的是被 Ctrl+C / 调度器抢占打断时
        # 也留得下「本趟走到了哪一段、翻了多少屏」。原先它在 `try` 外面，
        # 被打断就一个字都没有——而那正是 2026-08-18 排障时分不清「被掐」与
        # 「跑满」的原因之一。
        kept = keep_screens(screens, off_page=outcome != 0)
        say(
            completion_message(
                progress, written=written, suspect=written - len(kept), outcome=outcome
            )
        )
        # ⚠️ **没接上的屏单独报一行，不塞进 `completion_message`。**
        # 那句话是「这一趟干了多少」，这句是「这一趟有几屏没接上」——后者是异常
        # 信号，混进前者会被当成正常统计扫过去。为 0 时不打，省得每趟都有一行噪声。
        if screens_without_overlap:
            say(
                f"⚠️ 本趟有 {screens_without_overlap} 屏与上一屏没有共同坐标"
                f"（重叠可能断了；中间的行没被读过，事后判据救不了）"
            )
            record_log("采集重叠断裂", {"screens_without_overlap": screens_without_overlap})
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
        "--blind-rows",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"开榜后先用滚轮连拨 N 行再开始检测 bot；不传就用默认的 {BLIND_SCROLL_ROWS} 行。"
            "0 = 一格都不拨（最保守）"
        ),
    )
    parser.add_argument(
        "--blind-scrolls",
        type=int,
        default=None,
        metavar="N",
        help=(
            f"【回滚用】盲滚段退回慢拖 N 屏那条老路；不传 N 就用默认的 {BLIND_SCROLLS} 屏。"
            "只在没给 --blind-rows 时才生效"
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

    # ⚠️ **优先级要显式写出来，不能靠「谁不是 None」撞出来。** 两个旋钮同时存在是
    # 有意的：`--blind-rows` 是滚轮那条新路，`--blind-scrolls` 留着当**一键回滚**
    # （只传它就退回慢拖，不必改代码、不必重新发版；道理写在
    # `storage.models.MilitaryAttackConfigRow.blind_scroll_rows` 上）。
    # 两个都给时行数赢：那才是这次改动的主路，而回滚是要显式选的。
    if args.blind_rows is not None:
        blind_rows: int | None = args.blind_rows
        blind_rows_source = "cli"
    elif args.blind_scrolls is not None:
        # 只给了屏数 = 明确要走老路。行数置 None 就是这个意思。
        blind_rows = None
        blind_rows_source = "cli"
    else:
        # 不传就是 `BLIND_SCROLL_ROWS` 那个常量本身，不是另写一个「看起来一样」的
        # 数字：默认值只该有一处。
        blind_rows = BLIND_SCROLL_ROWS
        blind_rows_source = "default"

    return run_with_foreground_guard(
        lambda: scan(
            RankingColumns(
                rank=pair(args.rank_column, default.rank),
                name=pair(args.name_column, default.name),
                score=pair(args.score_column, default.score),
            ),
            bot_limit=args.bot_limit,
            blind_rows=blind_rows,
            blind_rows_source=blind_rows_source,
            blind_scrolls=BLIND_SCROLLS if args.blind_scrolls is None else args.blind_scrolls,
        )
    )


def _rank_of(text: str) -> int | None:
    match = re.search(r"\d+", text)
    return int(match.group()) if match is not None else None


def _prepared(cell: Any) -> Any:
    """灰度、**不二值化**、3× LANCZOS。逐格与成批两条路共用同一份预处理。

    ⚠️ 拆出来是为了让两条路喂给 tesseract 的像素**逐字节相同**——那是成批读
    唯一站得住的前提（见 `_read_cells`）。
    """
    from PIL import Image

    return cell.convert("L").resize((cell.width * 3, cell.height * 3), Image.Resampling.LANCZOS)


def _read_cell(cell: Any, ocr: Any, *, single_line: bool = True) -> str:
    """读一格。单格用 `--psm 7`，整条列用 `--psm 6`。"""
    config = "--psm 7" if single_line else "--psm 6"
    return str(ocr.image_to_string(_prepared(cell), lang="eng", config=config)).strip()


#: 成批读一列时，tesseract 最多跑多久。超时就退回逐格读。
#:
#: 一列 13 格实测 0.16 秒，给到 60 秒是纯粹的兜底——挂机整夜时一个卡住的子进程
#: 会把整条链路停摆，而退回逐格读只是慢 7 倍。
CELL_BATCH_TIMEOUT_S = 60.0


def _read_cells(cells: list[Any], ocr: Any) -> list[str]:
    """读一整列的格子：**一次进程，逐格独立**。读不成就原样退回逐格调用。

    ## ⚠️ 为什么不是「把格子拼成一张图读一次」

    贵的不是「分开读」，是**启动进程**。2026-08-23 本机实测：

        整屏读（三列逐格，40 次调用）    3.96 秒
        40 次单格调用                    4.10 秒  → 每次 102 毫秒
         1 次整列调用（像素量还更大）    0.25 秒
        ⇒ 固定开销占整屏读的 **97%**

    所以第一反应是把 13 个格拼进一张画布读一次。**试了，不行**：横向拼 + `--psm 7`
    与逐格读有 **22.6%** 的格不一致，竖向拼 + `--psm 6` 有 **49.5%**。根子是
    tesseract 的二值化与字号统计是**对整幅图**算的——13 个格进同一张图，全局统计
    就变了，于是「像素逐字节相同」并不等于「结果相同」。

    而且差异里有**致命的一类**：`bot_2_470_11` 被拼图读成 `bot_2_470_1`。
    `_1` 是个合法位置，反解得出坐标、区间校验放过——舰队会飞到一个错的星球。
    和整列读栽在同一个坑（见 `locate_rows`）。

    ## 成立的做法：tesseract 的图片清单

    CLI 支持传一个清单文件（每行一个图片路径），**清单里每张图各自独立走完整条
    流水线**，输出用换页符分页。于是逐格独立与一次 spawn 同时拿到。

    2026-08-23 语料实测（15 屏 × 3 列 = 558 格）：**0 个不一致，快 7.3×**
    （每列 1.19 秒 → 0.16 秒）。

    ⚠️ **退回逐格读的两个口子都必须留着**：拿不到 tesseract 可执行文件时
    （单元测试注入的假 `ocr` 就是这种），以及子进程出错/超时时。退回只是慢，
    而读不出来是要丢数据的。
    """
    if not cells:
        return []
    exe = getattr(getattr(ocr, "pytesseract", None), "tesseract_cmd", None)
    if not exe:
        return [_read_cell(cell, ocr) for cell in cells]
    try:
        return _tesseract_batch(str(exe), [_prepared(cell) for cell in cells])
    except (OSError, subprocess.SubprocessError, ValueError):
        return [_read_cell(cell, ocr) for cell in cells]


def _tesseract_batch(exe: str, images: list[Any]) -> list[str]:
    """把已经预处理好的格子交给 tesseract 的清单模式，返回**与入参等长**的文本。

    ⚠️ **返回长度必须和入参对齐**，不足补空串：调用方按下标把结果配回行，
    少一页就会整列错位——那种错不会报错，只会把军力配到别人名下。
    """
    with tempfile.TemporaryDirectory(prefix="evo-ocr-") as folder:
        root = pathlib.Path(folder)
        paths = []
        for index, image in enumerate(images):
            target = root / f"{index:03d}.png"
            image.save(target)
            paths.append(str(target))
        listing = root / "cells.txt"
        listing.write_text("\n".join(paths), encoding="utf-8")
        completed = subprocess.run(
            [exe, str(listing), "stdout", "-l", "eng", "--psm", "7"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            # ⚠️ OCR 出来的字什么都可能有，编码错误不许弄死进程（同 `say` 那条账）。
            errors="replace",
            timeout=CELL_BATCH_TIMEOUT_S,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(f"tesseract 清单模式返回 {completed.returncode}")
        # 清单模式用**换页符**分页，一张图一页，顺序与清单一致。
        pages = [page.strip() for page in completed.stdout.split("\f")]
        pages = pages[: len(images)]
        return pages + [""] * (len(images) - len(pages))


__all__ = [
    "BlindSpinAccount",
    "BlindWalk",
    "HumanStretch",
    "NameSample",
    "RankingColumns",
    "ScanProgress",
    "ScanStage",
    "blind_spin_payload",
    "completion_message",
    "drag_blind_rows",
    "exit_code_for_stretch",
    "progress_mark",
    "is_self_row",
    "enter_game_exit_code",
    "keep_screens",
    "name_column_text",
    "name_excerpt",
    "read_name_column_confirming",
    "coordinates_of",
    "release_stuck_mouse",
    "next_score_anchor",
    "renderable_score",
    "screen_scores",
    "main",
    "parse_score",
    "position_from_image",
    "report_blind_spin",
    "report_bot_area_reached",
    "locate_rows",
    "rows_from_image",
    "sample_overlap",
    "scroll_through_humans",
    "spin_blind_rows",
    "targets_from_rows",
    "take_batch_targets",
    "track_progress",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
