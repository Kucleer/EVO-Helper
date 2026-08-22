"""军力排行榜：走到「排名 → 军事评分」，并往下滚。**只管路径，不管解析。**

薄薄一层：**每一步都先认出这一屏，认出来了才做下一个动作**。
一行榜单都不解析——哪一列是分数、谁排第几、这一屏是不是军事榜，
全在 `domain.ranking`（本层通过注入的回调去问它）。

    任意一屏
      → 拖底部导航（横向）→ 露出 `太空舱 商店 联盟 排名 设置`
      → **按文本**找「排名」→ 点它当屏读到的那个 x
      → 排行榜面板（默认停在**经济评分**）→ **点一次**「军事评分」→ 回读确认还在榜上
      → `scroll_once()` 一屏一屏往下

## 三条规矩

1. **空结果不是证据。** 拖动中有加载动画，那时读到的是半屏或全空。
   所以每一处「读一屏」都走 `_rows_confirming` / `_labels_confirming`：
   空清单重读几次再认。同 `preset_picker.read_names_confirming`——
   那一条是实机通宵白跑两小时换来的。
2. **滚到底的判据是「拖了一下内容没变」**，不是拖固定次数
   （先例：`domain.planet_switch.list_exhausted`）。
3. **会断线。** 这一层不负责重连（那是 `game.session_keeper` 的事），
   但要把「我已经不在榜单页上了」认出来并如实报告
   （`ScrollOutcome.OFF_PAGE` / `RankingNotReached`），而不是继续对着别的画面拖。

## `read_rows` 的契约：认不出的行必须丢掉

调用方交出来的必须是**认得出是榜单行**的东西；认不出的一律丢掉，不留占位。
照 `domain.planet_switch.rows_from_words` 的做法。这样本层才能把
「读到 0 行」直接当成「我不在榜单页上（或者还在加载）」——
这是本层唯一一份「还在不在榜单上」的判据，也是断线能被认出来的原因。

**一行长什么样，这一层不关心**（所以类型是参数化的 `RowT`，实机上就是
`domain.ranking.RankingRow`）。它只对这些行做三件事：看空不空、
跟上一屏比相等、交给调用方解析。原样传出去而不是压成字符串，
是为了让调用方拿到的就是它自己解析好的那份，不必为了配合本层再读一遍
——那一遍是另一帧画面，两帧之间列表可能已经动过。

## 分步慢拖写在这一层

**不 import `tools.pirate_loop.slow_drag`**：那是跨层依赖（`tools` 依赖 `game`，
不能反过来）。所以驱动面上要的不是一个 `drag`，而是
`press` / `move_to` / `release` 三个原语，分步由这里做——见 `_slow_drag`。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from evo_helper.domain.text import snap_to_vocabulary
from evo_helper.game.ranking_ui import (
    CLOSE_ATTEMPTS,
    DRAG_PRESS_HOLD_S,
    DRAG_RELEASE_HOLD_S,
    DRAG_STEPS,
    GLIDE_SETTLE_S,
    MAX_SPIN_ROUNDS,
    MILITARY_TAB,
    NAV_BAR_Y,
    NAV_DRAG_FROM_X,
    NAV_DRAG_TO_X,
    NAV_DRAG_WAIT_S,
    NAV_LABEL_MAX_DISTANCE,
    NAV_LABEL_ROI,
    NAV_LABEL_UPSCALE,
    NAV_LABEL_WORD_GAP_PX,
    NAV_LABELS,
    NAV_MAX_DRAGS,
    PANEL_OPEN_WAIT_S,
    PANEL_READY_ROW_SLACK,
    PANEL_REOPEN_ATTEMPTS,
    RANKING_CLOSE,
    RANKING_LABEL,
    READ_ATTEMPTS,
    REREAD_WAIT_S,
    ROWS_PER_NOTCH,
    ROWS_PER_NOTCH_MAX,
    ROWS_PER_SCREEN,
    SCROLL_FROM_Y,
    SCROLL_SETTLE_WAIT_S,
    SCROLL_TO_Y,
    SCROLL_X,
    SPIN_FINAL_APPROACH_ROWS,
    SPIN_TOLERANCE_ROWS,
    TAB_SWITCH_WAIT_S,
    WHEEL_GAP_S,
)


class RankingDriver(Protocol):
    """本层需要的最小操作面。真实实现驱动鼠标，测试里换成假的。

    `press` / `move_to` / `release` 是**分步慢拖**要的三个原语：
    一步式的 `dragTo` 会被游戏面板当成点击（`tools.pirate_loop.slow_drag`
    的注释里记着这条实测），而分步这件事必须发生在 `game` 层，
    否则就得反过来 import `tools`。`wheel_notch` 同理——见它自己的注释。
    """

    def click(self, x: int, y: int, *, label: str = ...) -> None: ...

    def press(self, x: int, y: int, *, label: str = ...) -> None: ...

    def move_to(self, x: int, y: int) -> None: ...

    def release(self) -> None: ...

    def wait(self, seconds: float) -> None: ...

    def hover(self, x: int, y: int) -> None:
        """把指针挪到某处，**不按下**。盲滚之前用它落点。

        ⚠️ 不能用 `move_to` 代替：那是慢拖途中的一步，没按下就调会被守卫拦掉。
        """
        ...

    def wheel_notch(self) -> None:
        """往下滚**一格**（`dwData = -WHEEL_DELTA`）。

        ⚠️ 协议上只有「一格」这一种粒度，是**有意的**：允许传格数的话，
        实现里迟早会把 N 格合成一个大事件发出去，而那会被游戏封顶
        （实测 800 格只走 14px），且封顶是静默的——事件发出去了、列表没走。
        格数与间隔的密度由 `RankingNavigator.spin_blind` 控制，
        理由同分步慢拖：循环必须留在 `game` 层，否则就得反过来 import `tools`。
        """
        ...


class RankingNotReached(RuntimeError):
    """没能确认自己站在军事排行榜上。

    **不许退而求其次接着往下走**：切错页签拿到的是经济榜——bot 全 0 分、
    按坐标顺序排，和军事榜是完全不同的一套数据。实机 2026-08-14 就因为拿错页签，
    把一整套结论建立在错的数据上。认不出宁可这一轮不采。
    """


class ScrollOutcome(Enum):
    """滚一屏的结局。

    `EXHAUSTED` 与 `OFF_PAGE` 分开而不是合并成一个 False：前者是**榜单读完了**
    （正常收尾），后者是**画面已经不是榜单了**（多半掉线）。
    对调用方的意思完全不同：前者该收工入库，后者该去重连、并且这一轮读到的
    最后几屏都要当可疑的看。
    """

    SCROLLED = "scrolled"
    EXHAUSTED = "exhausted"
    OFF_PAGE = "off_page"


@dataclass(frozen=True)
class ScrollStep[RowT]:
    """滚一屏的结果：结局 + **拖完之后**这一屏读到的行。

    行一并交出来是为了让调用方不必再读一次——那一次重读既费一遍 OCR，
    又是另一帧画面，两帧之间列表可能已经动过。
    """

    outcome: ScrollOutcome
    rows: tuple[RowT, ...]


@dataclass(frozen=True, slots=True)
class SpinResult:
    """一趟滚轮盲滚的账。

    ⚠️ **`rows_measured` 是这一层量出来的，不再由调用方填。** 2026-08-22 之前
    这里写着「拨完就返回、一行都不读」，那是开环时代的说法——闭环之后本层每一轮
    都要读一次位置才知道要不要补拨，于是「走了多少行」在这里就是现成的**测量值**。
    调用方（`tools.ranking_scan`）照抄它进 `system_log` 即可，别再自己另测一遍：
    那是另一帧画面，两帧之间列表可能已经动过。

    `rows_measured is None` 只有一个含义：**这一趟没有闭环保护**（起点读不出来，
    退回开环一次性拨完）。它不是「零行」，也不是「没测」——所以日志上必须
    照实说「测不出」，不许拿格数换算出一个数来冒充测量值。

    `spin_seconds` 是**实测**拨完用了多久（只算发滚轮事件那些时间，不含每轮读屏
    和等滑行），记它是为了让「每格 16ms」这条可验证：`WHEEL_GAP_S` 走的是
    `driver.wait`，而 Windows 上 `time.sleep` 的粒度是 15.6ms——真被撑成 31ms/格
    的话，动量就攒不起来，而症状同样是「拨了但没走」。把用时记进 `system_log`
    才看得出这件事。

    `rates` 是每一轮观测到的行/格。留着整串而不是只留个平均：这一族数的**散布**
    才是要害（实测 0.49–1.25），压成一个平均数正好把它抹掉。
    """

    rows_requested: int
    notches: int
    spin_seconds: float
    #: 实测走了多少行；`None` = 这一趟是开环退路，没测。
    rows_measured: int | None
    #: 拨了几轮（开环退路记 1；`rows == 0` 记 0）。
    rounds: int
    #: 每轮观测到的行/格。开环退路是空的——那一趟一次都没量。
    rates: tuple[float, ...]


@dataclass
class RankingNavigator[RowT]:
    """把画面带到军事排行榜上，并一屏一屏往下滚。

    回调都由调用方注入，**这一层不认识 OCR，测试里也就不需要假图片**
    （同 `game.planet_list.PlanetSwitcher`）：

    - `read_labels`：底部导航标签行的 `(中心 x, 文字)` 词框。
      那个 x 就是待会儿要点的地方——所以必须是**这一屏**读出来的。
    - `read_rows`：当前这一屏的榜单行，**认不出的丢掉**（见模块头的契约）。
      行是什么类型由调用方定，实机上是 `domain.ranking.RankingRow`。
    - `row_has_score`：这一行的分数解析出来了没有。**注入而不是 `row.score`**：
      本层不认识行的形状（`RowT` 是参数化的），认识的是调用方。
      实机上就是 `lambda row: row.score is not None`。
      它只在 `_switch_to_military` 那道「面板铺开了没有」的闸上用一次，
      **不是**用来判「我在哪个页签上」——那个判据不存在，见 `_switch_to_military`。
    - `read_position`：**现在滚到第几名了**，读不出就 `None`。只服务于
      `spin_blind` 的闭环——那一段唯一要回答的就是这一个问题。

      ⚠️ **它答的是「在第几名」，不是「交出几行」，这个形状是有意的。**
      2026-08-22 实机：闭环原先拿 `read_rows()` 交出来的行取名次中位数，
      而那条路是**按行网格逐行裁剪**的（`ROW_FIRST_Y + k×ROW_PITCH_PX`）——
      滚轮会把列表停在**非整行位置**（实测偏离网格约 12px），逐行裁剪就横跨
      两行、名次全糊，于是请求 500 行时第一轮拨完当场「读不出名次」、闭环失效。
      所以盲滚要一个**与行对齐无关**的读数器（实机上是
      `tools.ranking_scan.position_from_image`：整列一次读完），
      而这一层只要求它能直接答出名次，怎么读不管。

    ⚠️ **没有「这一屏是不是军事榜」这个回调**，因为那个判据不存在——见
    `_switch_to_military`。想看哪个榜就点哪个页签。
    """

    driver: RankingDriver
    read_labels: Callable[[], Sequence[tuple[int, str]]]
    read_rows: Callable[[], Sequence[RowT]]
    row_has_score: Callable[[RowT], bool]
    read_position: Callable[[], int | None]
    say: Callable[[str], None] = print

    # -- 进榜单 -------------------------------------------------------------

    def open_military_ranking(self) -> tuple[RowT, ...]:
        """走到军事排行榜，返回**回读确认过、而且确认已经铺开**的第一屏。

        每一步都先认出这一屏再点下一下；认不出就抛 `RankingNotReached`，不盲点。

        ⚠️ **首屏没铺开时，先关掉重开一次，而不是当场把整趟扔掉。** 生产 08-17
        17:36:02 那一趟读到的首屏只有 5 行、头一行是 `name='> <A,'`、`score=None`
        ——面板还没渲染完。那一屏顺利过了当时那道「非空即可」的闸，然后盲翻
        101 屏一无所获，**白跑约 7 分钟**。而重开一遍只要十几秒。

        真断线时这个重开也不会变成瞎点：`_close_and_reopen` 点完 ✕ 之后要重走
        `_reveal_ranking_label`，读不出标签行照样抛。
        """
        for attempt in range(PANEL_REOPEN_ATTEMPTS):
            if attempt:
                self._close_and_reopen(attempt)
            x = self._reveal_ranking_label()
            self.driver.click(x, NAV_BAR_Y, label=RANKING_LABEL)
            self.driver.wait(PANEL_OPEN_WAIT_S)
            opened = self._rows_confirming()
            if not opened:
                raise RankingNotReached(
                    f"点完「{RANKING_LABEL}」之后一行榜单都读不出来："
                    "面板没开出来，或者已经不在榜单页上了（比如断线）"
                )
            del opened  # 打开时那一屏是经济榜，没有用；真正要的是切过去之后那一屏
            rows = self._switch_to_military()
            if rows is not None:
                return rows
        raise RankingNotReached(
            f"重开 {PANEL_REOPEN_ATTEMPTS} 次面板，首屏仍然不像一张铺开的榜单"
            f"（要么行数不到 {ROWS_PER_SCREEN - PANEL_READY_ROW_SLACK}，"
            "要么一行分数都解析不出来）；不再往下盲翻"
        )

    def _close_and_reopen(self, attempt: int) -> None:
        """首屏没铺开时的退路：点掉 ✕，让下一轮从头再开一次面板。

        ⚠️ **这里可以点 ✕，是因为已经读到过榜单行了。** 「在认不出的画面上不许
        按下手指」这条（见 `_reveal_ranking_label`）没有被破：走到这一步意味着
        `_switch_to_military` 至少读出了一行，也就是**确认过面板就开在眼前**，
        只是没铺开。这也正是「读到 0 行」那一支反而当场抛、不重开的原因——
        那一支连「面板在不在」都不知道，`RANKING_CLOSE`(750, 71) 落在哪儿没人知道。
        """
        self.say(f"  关掉面板准备第 {attempt + 1} 次开榜")
        self.driver.click(*RANKING_CLOSE, label="关闭排行榜（重开前）")
        self.driver.wait(NAV_DRAG_WAIT_S)

    def _switch_to_military(self) -> tuple[RowT, ...] | None:
        """点一次「军事评分」，回读确认**面板铺开了**。铺开了给行，没铺开给 None。

        ⚠️ **面板打开时停在「经济评分」**（用户 2026-08-14 实测），所以这一下是必须的，
        不是可选的兜底。

        ⚠️ **页签是幂等的按钮，不是开关**（用户实测：已经在军事榜上再点一次不会切回去）。
        所以用户的口径是「你不用管现在是什么，你需要看什么，就点什么切换」——
        先点再说比先判断再点简单，而且**不需要一个单屏判据**。

        ⚠️ **那个单屏判据本来就不存在。** 这里原先有个 `on_military_board` 回调，
        实机上用的判据是「读到任何非零分数就说明在军事榜」。2026-08-14 当场证伪：
        两个页签的榜首十三行都是真人、分数都在 404.17M 这个量级，非零判据两边都成立。
        「经济榜 bot 全是 0」只对第 639 名之后成立，而看到那一段要先滚六十屏。

        ## 下面这道闸判的**不是页签，是渲染**

        它回答的是「这个面板到底铺开了没有」，和「我在哪个页签上」毫无关系——
        两个页签铺开之后长得一样，都会过这道闸。**别把它误读成又一次尝试判页签**，
        那个判据上面刚说过，不存在。

        判据两半，缺一不可：

        1. **行数接近满屏**（`ROWS_PER_SCREEN - PANEL_READY_ROW_SLACK`，当前 12）。
           生产 22 条日志里 21 条是 13 行、1 条是 5 行，**双峰、中间没有灰区**。
        2. **至少一行解析得出分数**。这一条才是真正区分那一屏坏屏的东西：
           它的头一行是 `RankingRow(rank=None, name='> <A,', score=None, ...)`
           ——名字是噪声、分数解析不出来，而这种半渲染的屏**行数也可能凑够**。

        ⚠️ **这不违反模块头第 1 条「空结果不是证据」。** 那一条针对的是**单帧**，
        而这道闸站在 `_rows_confirming` 之内——它已经按 `READ_ATTEMPTS`(3) ×
        `REREAD_WAIT_S`(0.6s) 重读过了。所以这里看到的不是「一帧的抖动」，
        是「重读几次之后仍然只有这么多」。何况不达标也不是当场判死，
        是退回去重开一次（`open_military_ranking`）。

        读到 0 行仍然当场抛而不重开：那时连「面板在不在」都不知道，
        见 `_close_and_reopen`。
        """
        self.driver.click(*MILITARY_TAB, label="军事评分")
        self.driver.wait(TAB_SWITCH_WAIT_S)
        rows = self._rows_confirming()
        if not rows:
            raise RankingNotReached(
                "点完「军事评分」之后一行榜单都读不出来；"
                "画面已经不是排行榜面板了（比如断线），不再点下去"
            )
        minimum = ROWS_PER_SCREEN - PANEL_READY_ROW_SLACK
        scored = sum(1 for row in rows if self.row_has_score(row))
        if len(rows) < minimum or not scored:
            # 挡掉的这一刻要说清**为什么**挡 + **当时看到了什么**（CLAUDE.md 的日志口径）。
            # 08-17 那次故障之所以拖了两天，正是因为日志只说了行数、没说这一屏长什么样。
            self.say(
                f"  首屏不像铺开的榜单：{len(rows)} 行（要 ≥{minimum}）、"
                f"{scored} 行读得出分数（要 ≥1），头一行 {rows[0]!r}"
            )
            return None
        self.say(f"  已切到军事榜（这一屏 {len(rows)} 行，头一行 {rows[0]!r}）")
        return rows

    def _reveal_ranking_label(self) -> int:
        """把「排名」拖出来，返回它**这一屏**的中心 x。

        ⚠️ 返回的 x 只能来自这一次的 OCR。写死 1079 的话，条停的位置差一点，
        点下去就是「联盟」或「设置」——用户口径是「识别文本进行定位，
        而不是直接定位」（见 `preset_picker` 模块头）。

        ⚠️ 标签一个都读不出来时**不拖也不点**：那说明这一屏根本不是带导航条的画面
        （被浮层盖住、掉线、或者排行榜面板已经开着）。在认不出的画面上按下手指、
        拖一把，落点是什么谁也不知道。
        """
        seen: list[list[str]] = []
        for attempt in range(NAV_MAX_DRAGS + 1):
            labels = self._labels_confirming()
            if not labels:
                raise RankingNotReached(
                    "底部导航的标签一个都读不出来：认不出这一屏，不拖也不点"
                    f"（已经拖了 {attempt} 次，逐次读到的是 {seen}）"
                )
            runs = merged_labels(labels)
            seen.append([text for _x, text in runs])
            hit = ranking_label_x(runs)
            if hit is not None:
                return hit
            if attempt == NAV_MAX_DRAGS:
                break
            # ⚠️ 这里**不是**点 `pirate_ui.NAV_SCROLL_RIGHT`(1204, 862)。
            # 实机先点了它，导航条纹丝不动——这一段是拖出来的，不是点箭头翻页。
            self._slow_drag(
                NAV_DRAG_FROM_X, NAV_BAR_Y, NAV_DRAG_TO_X, NAV_BAR_Y, label="导航条左移"
            )
            self.driver.wait(NAV_DRAG_WAIT_S)
        raise RankingNotReached(f"底部导航上找不到「{RANKING_LABEL}」；逐次读到的是 {seen}")

    # -- 滚 -----------------------------------------------------------------

    def scroll_once(self) -> ScrollStep[RowT]:
        """往下滚一屏，如实说出「滚动生效了没有」。

        拖之前先读一次（而不是复用调用方手里的上一屏）：这既是「到底了没有」的
        比较基准，也是**按下手指之前的最后一次确认**——同 `game.action_guard`
        的「点击前重新观察」。读不出行就一根手指都不放上去。
        """
        before = self._rows_confirming()
        if not before:
            self.say("  滚动前一行都读不出来：已经不在榜单页上了，不拖")
            return ScrollStep(ScrollOutcome.OFF_PAGE, ())
        self._slow_drag(SCROLL_X, SCROLL_FROM_Y, SCROLL_X, SCROLL_TO_Y, label="榜单下滚")
        self.driver.wait(SCROLL_SETTLE_WAIT_S)
        after = self._rows_confirming()
        if not after:
            # ⚠️ **不能判成「到底了」。** 拖之前明明读得出，拖之后读不出，
            # 那不是榜单读完了，是画面变了（实机一小时断了三次，其中一次正好
            # 断在第 60 名上）。判成到底就会把半截榜单当成完整榜单收工。
            self.say("  拖完之后一行都读不出来：已经不在榜单页上了")
            return ScrollStep(ScrollOutcome.OFF_PAGE, ())
        if list(after) == list(before):
            self.say(f"  拖了一下内容没变（{len(after)} 行）：到底了")
            return ScrollStep(ScrollOutcome.EXHAUSTED, after)
        return ScrollStep(ScrollOutcome.SCROLLED, after)

    def scroll_blind(self) -> None:
        """拖一屏，**不读也不判**。翻长段时用。

        ⚠️ 与 `scroll_once` 的差别只有一处：不在按下手指之前重读一遍。
        代价由调用方补上——它每拖完一次都要自己读一次并确认还在榜单上，
        于是「只在刚确认过的画面上按下手指」这条不变式仍然成立，
        只是确认发生在**上一轮的末尾**而不是这一轮的开头。

        为什么值得省这一次：翻真人段要 73 屏（bot 从第 ~587 名才开始，
        实测 8 名/滚），而 `scroll_once` 每屏读两遍。那一段里唯一要回答的问题
        是「到 bot 区了没有」——一次廉价的整列 OCR 就够，不必读两遍全表。
        """
        self._slow_drag(SCROLL_X, SCROLL_FROM_Y, SCROLL_X, SCROLL_TO_Y, label="榜单下滚")
        self.driver.wait(SCROLL_SETTLE_WAIT_S)

    def spin_blind(self, *, rows: int) -> SpinResult:
        """连拨滚轮走过 `rows` 行：**拨完读一次，不够就补拨**（闭环）。

        用户口径（2026-08-22）：「改成闭环：拨完读一次，不够就补拨」。

        ## 为什么不能开环

        原先这里是 `notches = round(rows / ROWS_PER_NOTCH)` 一次性拨完，
        精度全押在那一个标定上。**同一天实机把它证伪了**：两趟 × 5 组量到的
        行/格是 0.49–1.25（中位 0.96），且不随格数单调变化——按 1.08 换算的
        「盲滚 700 行」，真实推进落在 320–810 行之间，而偏差是**静默的**
        （见 `ranking_ui.ROWS_PER_NOTCH` 上那张表）。

        闭环把「走多远」从**推算**换成**测量**：每一轮拨完读一次名次中位数，
        差多少就补多少。标定值只剩下「第一轮拨多少」这一个用处。

        ## 从下方逼近：每一轮都故意走不到

        本轮格数 = `ceil(剩余 / rate_hi)`，`rate_hi` 是**本趟已观测到的最大**
        行/格（还没观测时才用 `ROWS_PER_NOTCH`）。

        ⚠️ **用最大速率是有意的，别改成平均或最小。** 除以一个偏大的速率会算出
        偏少的格数，也就是**故意走不到**，剩下的下一轮再补。而冲过头是**不可逆**
        的：这一段不读屏（读也只读名次中位数、不采数据），榜首被跳过去的那一批
        bot 不会报错、不会少一条日志，只是采回来的数静悄悄少一截。
        少走一点的代价则完全不同——由检测段接手，实测约 4.6 秒/屏。

        ⚠️ **除数取「本趟观测」与 `ROWS_PER_NOTCH_MAX`(1.25) 的较大者。**
        只取本趟观测是不够的：第一轮恰好抽到区间下端（0.49）时，第二轮会按 0.49
        算格数，而那一轮真实速率若跳回 1.25 就走出 2.5 倍距离、直接冲过头——
        **越是遇到慢的一轮，下一轮越危险**。
        （用一个偏大的速率当除数**不是**把精度押回被证伪的标定上：它只决定
        「这一轮少走多少」，走了多远仍旧由测量回答。两件事别混。）

        ## 最后几十行不补拨

        剩余不足 `SPIN_FINAL_APPROACH_ROWS`(30) 就收手，哪怕还没进
        `SPIN_TOLERANCE_ROWS`(8)。⚠️ **这不是懒，是因为那一轮是唯一会冲过头的
        地方**：2026-08-22 实机请求 200 行走了 218 行，18 行的超出全出在最后一轮
        （剩余只剩几行时，一格的粒度加惯性滑行压不住，而那一轮的分母只有几格，
        行/格的噪声大到离谱——逐轮读数 0.85 / 0.98 / **2.82**）。
        少走的几十行由检测段接手，多走的没有任何东西接得住。

        ## 三条不能动的东西

        ⚠️ **等待不是每格一次。** `scroll_blind` 那条路每屏都
        `wait(SCROLL_SETTLE_WAIT_S)`，70 屏就是 140 秒纯等待（生产实测整段
        294.6 秒）——盲滚改滚轮的收益**全部**来自把这些等待合并。每格都等一下
        就等于没改：实测那样 80 格只走 2 行。闭环之后等待是**每轮一次**
        （常态两三轮），不是每格一次。

        ⚠️ **不许把 N 格合成一个大事件。** 游戏对单个事件的幅度封顶（实测 800 格
        只走 14px），而封顶是静默的；动量靠的是**密集的独立事件**。所以驱动面上
        只有 `wheel_notch()` 这一种粒度，密度在这里控制。

        ⚠️ **每一轮拨完都要等滑行停**（`GLIDE_SETTLE_S`）才准读：不等就是在移动中
        的画面上逐行裁剪，读出来的名字横跨两行——而那既会让这一轮的测量作废，
        也会让紧接着的检测段读到糊的一屏。

        ## 三条退路（都不许把整趟判成失败）

        - **起点读不出来** → 退回**开环一次性拨完**，并说清这一趟没有闭环保护。
          ⚠️ 这条退路必须留：读不出就整趟不干，会让采集链路在 OCR 偶发失灵时
          直接瘫掉，而 OCR 偶发失灵是常态（滚轮把列表停在非整行位置）。
        - **某一轮之后读不出当前位置** → 就此收手，如实说走到哪儿了。
        - **某一轮一行都没走** → 停，不再重试。拨不动时多拨一百次也一样，
          而每轮要花 2.5 秒等滑行。

        `rows == 0` 是最保守的合法取值（「一格都别拨」），此时**一次事件都不发、
        一次等待都不做、一次屏都不读**——那样这一趟就退回成纯慢拖，也就是这次
        改动的一键回滚。负数不接受：往上滚会把已经翻过的榜单再翻一遍，
        而调用方拿不到任何提示。
        """
        if rows < 0:
            raise ValueError(f"盲滚行数不能是负数（拿到 {rows}）：往上滚不是这一段该做的事")
        if not rows:
            # 一键回滚这一支：连「我现在在第几名」都不读。读一次要一遍整屏 OCR，
            # 而这一支的语义就是「这一趟什么都别做」。
            return SpinResult(
                rows_requested=0,
                notches=0,
                spin_seconds=0.0,
                rows_measured=None,
                rounds=0,
                rates=(),
            )

        start = self.read_position()
        if start is None:
            return self._spin_open_loop(rows)

        target = start + rows
        current = start
        notches_total = 0
        spin_seconds = 0.0
        rates: list[float] = []
        rounds = 0
        position_lost = False
        for _round in range(MAX_SPIN_ROUNDS):
            remaining = target - current
            if remaining <= SPIN_TOLERANCE_ROWS:
                break
            if remaining < SPIN_FINAL_APPROACH_ROWS:
                # ⚠️ **最后那几十行不补拨——那是唯一会冲过头的地方。**
                # 2026-08-22 实机：请求 200 行走了 218 行，18 行的超出全出在最后
                # 一轮（剩余只剩几行时，一格的粒度加惯性滑行压不住，而那一轮的
                # 分母只有几格，`moved / notches` 噪声大到离谱——逐轮读数
                # 0.85 / 0.98 / 2.82，最后那个多半是测量假象）。
                # 少走这几十行由检测段逐屏接手（约 4 屏、18 秒）；冲过头则不可逆。
                self.say(
                    f"  盲滚只差 {remaining} 行（不足 {SPIN_FINAL_APPROACH_ROWS} 行）："
                    f"最后这一截不补拨了（补一轮反而是唯一会冲过头的地方），"
                    f"停在第 {current} 名、本趟走了 {current - start} 行，交给检测段"
                )
                break
            # 没有本趟观测时才用标定值——它只负责第一轮拨多远。
            # ⚠️ 除数取「本趟观测」与「已知最快」的**较大者**，两边缺一不可：
            # 只用本趟观测时，第一轮抽到区间下端（0.49）会让第二轮按 0.49 算格数，
            # 而真实速率跳回 1.25 就走出 2.5 倍距离、冲过头——越是遇到慢的一轮，
            # 下一轮越危险。只用常量时，游戏哪天变快了又跟不上。
            rate_hi = max([*rates, ROWS_PER_NOTCH_MAX])
            notches = math.ceil(remaining / rate_hi)
            rounds += 1
            spin_seconds += self._send_notches(notches)
            notches_total += notches
            self.driver.wait(GLIDE_SETTLE_S)
            mark = self.read_position()
            if mark is None:
                self.say(
                    f"  盲滚第 {rounds} 轮拨完读不出名次："
                    f"从第 {start} 名起走了至少 {current - start} 行（要 {rows} 行），"
                    "闭环到此为止，**实走多少行就此不可知**，剩下的交给检测段"
                )
                # ⚠️ **位置丢了就必须报「不知道」，不能报最后一次读到的差值。**
                # 这一轮已经发出去 `notches` 格了，列表几乎肯定走了几百行；
                # 而 `current` 停在上一次读得出的地方，`current - start` 会说成
                # 「走了 0 行」。那是**一个断言**，而真相是「不知道」——
                # 这个数一路喂进「翻了 N 行到达 bot 区」，也就是自标定的输入，
                # 拿 0 去喂会把标定往「一格都走不动」的方向拽。
                # 2026-08-22 实机：请求 500 行、真的拨了 400 格，报回来的是 0。
                position_lost = True
                break
            moved = mark - current
            if moved <= 0:
                # ⚠️ **这一支不把 `current` 挪到 `mark` 上。** 名次中位数带一两行的
                # 抖动，`mark` 偶尔会比上一轮还小；采纳它会让 `rows_measured` 变成
                # 负数，而那个数一路喂进「翻了 N 行到达 bot 区」——自标定的输入。
                # 「没往前走」这件事已经如实说出来了，不必再用一个假的负数去表达。
                self.say(
                    f"  盲滚第 {rounds} 轮发了 {notches} 格，名次一行没往前走"
                    f"（拨完读到第 {mark} 名，拨之前是第 {current} 名；本趟共走"
                    f" {current - start} 行、要 {rows} 行）：拨不动时多拨几轮也一样，就此收手"
                )
                break
            current = mark
            rates.append(moved / notches)
        else:
            if target - current > SPIN_TOLERANCE_ROWS:
                self.say(
                    f"  盲滚拨满 {MAX_SPIN_ROUNDS} 轮仍差 {target - current} 行"
                    f"（要 {rows} 行、实走 {current - start} 行，停在第 {current} 名）："
                    "不再补拨，剩下的交给检测段"
                )
        return SpinResult(
            rows_requested=rows,
            notches=notches_total,
            spin_seconds=spin_seconds,
            rows_measured=None if position_lost else current - start,
            rounds=rounds,
            rates=tuple(rates),
        )

    def _spin_open_loop(self, rows: int) -> SpinResult:
        """起点读不出来时的退路：按标定一次性拨完，**这一趟没有闭环保护**。

        ⚠️ **必须留这条路，不许改成「读不出就不滚」。** 位置读数器整列读一次、
        抓 `[N]` 取中位数，抓不够几个就如实交 `None`（榜首三名是奖章图标、
        本来就没有 `[N]`，开榜那一屏尤其少）。让一次 OCR 失灵瘫掉整条采集链路，
        比这一趟少了保护坏得多。

        ⚠️ **`rows_measured` 留 `None`，不许拿格数换算一个数填进去。** 那正是这次
        改动要消灭的东西：`格数 × 1.08` 读起来像证据，其实是推算，而真实速率在
        0.49–1.25 之间抽。测不出就得在日志上明说测不出。
        """
        self.say(
            f"  盲滚 {rows} 行：起点名次读不出来（整列读数器交了「不知道」），"
            f"这一趟退回开环、按标定 {ROWS_PER_NOTCH} 行/格一次性拨完——"
            "**没有闭环保护**，实走多少行测不出"
        )
        notches = round(rows / ROWS_PER_NOTCH)
        spin_seconds = self._send_notches(notches)
        if notches:
            self.driver.wait(GLIDE_SETTLE_S)
        return SpinResult(
            rows_requested=rows,
            notches=notches,
            spin_seconds=spin_seconds,
            rows_measured=None,
            rounds=1,
            rates=(),
        )

    def _send_notches(self, notches: int) -> float:
        """拨 `notches` 格，返回**实测**用了多久（不含等滑行、不含读屏）。

        ⚠️ **先把指针挪进榜单里，滚轮才有地方去。**

        2026-08-22 生产事故：原先这里什么都不做，注释里写着「落点由上一个动作
        负责——`open_military_ranking` 之后指针停在面板内部」。那句话是真的，
        但**面板内部 ≠ 可滚动列表内部**：那一路最后点的是 `MILITARY_TAB`
        (1084, 212)，而列表从 `ROW_FIRST_Y`(257) 才开始——指针停在列表**上方
        45 像素**的页签条上。浏览器把滚轮事件路由给指针底下的元素，于是几百个
        事件全发给了页签条，**榜单一行都没动**，而日志照样说「盲滚 700 行」。

        落点用 `(SCROLL_X, SCROLL_FROM_Y)`——慢拖按下去的就是这一点，
        它落在列表里是被这条链路验证过无数次的既有事实，不是新猜的坐标。
        每一轮都挪一次而不是整趟挪一次：读屏那一步不碰鼠标，但**别把「指针没人
        动过」当成不变式**——那正是上面那次事故的成因。挪一次的代价是零。
        """
        if not notches:
            return 0.0
        self.driver.hover(SCROLL_X, SCROLL_FROM_Y)
        started = time.monotonic()
        for _notch in range(notches):
            self.driver.wheel_notch()
            self.driver.wait(WHEEL_GAP_S)
        return time.monotonic() - started

    # -- 收尾 ---------------------------------------------------------------

    def close(self) -> bool:
        """关掉面板，并把底部导航条**还原回左段**；还原确认了才返回 True。

        ⚠️ 还原不是洁癖。拖之前 `pirate_ui.NAV_PLANET`(840, 862) 是「行星」，
        拖之后同一个像素上坐着的是**「太空舱」**（拖完中心在 830，差 10px），
        而 `pirate_ui` 里写着那个东西点开是材料仓库、还会把整条导航条盖住。
        把条留在右段就交回控制权，等于给下一条链路埋了一颗雷。

        ⚠️ **点完 ✕ 必须先回读确认关掉了，才准拖。** 实机 2026-08-15 撞到过：
        第一次点 ✕ 没生效（上一个进程被杀时把鼠标左键按着留下了，那之后第一次
        点击的 mouseDown 是空操作），而代码不回读就往下拖——`NAV_BAR_Y = 862`
        落在**还开着的面板内部**，把榜单又滚了两行，然后回读必然失败。

        判据是现成的：面板盖住标签行（实测），所以**标签行读得出东西就等于面板
        已经关了**。读不出来就再点一次 ✕，而不是硬着头皮往下拖。

        最后的还原判据同样是回读：标签行读得出东西，而且里面**没有「排名」**。
        读不出来就如实返回 False——「读到 0 个标签」永远不算证据（模块头第 1 条）。
        """
        for attempt in range(CLOSE_ATTEMPTS):
            self.driver.click(*RANKING_CLOSE, label="关闭排行榜")
            self.driver.wait(NAV_DRAG_WAIT_S)
            if self._labels_confirming():
                break
            self.say(f"  点了 {attempt + 1} 次 ✕，标签行仍读不出来：面板还开着，再点一次")
        else:
            self.say("  面板关不掉；**不拖导航条**——那一拖会落在面板里，白拖还会滚乱榜单")
            return False

        self._slow_drag(NAV_DRAG_TO_X, NAV_BAR_Y, NAV_DRAG_FROM_X, NAV_BAR_Y, label="导航条右移")
        self.driver.wait(NAV_DRAG_WAIT_S)
        runs = merged_labels(self._labels_confirming())
        if not runs:
            self.say("  还原导航条之后标签读不出来；不敢说已经还原了")
            return False
        if ranking_label_x(runs) is not None:
            self.say(f"  导航条还停在右段（仍然读到「{RANKING_LABEL}」）")
            return False
        return True

    # -- 读一屏（空结果重读） -----------------------------------------------

    def _rows_confirming(self) -> tuple[RowT, ...]:
        """读这一屏的榜单行，**空结果要重读几次再认**。

        拖动中有加载动画（用户实机口径），那一帧读到的是半屏或全空。
        单帧的空结果是抛硬币，不是证据——同
        `preset_picker.read_names_confirming` / `vision.scan_reading.read_panel_confirming`。
        """
        for _attempt in range(READ_ATTEMPTS):
            rows = tuple(self.read_rows())
            if rows:
                return rows
            self.driver.wait(REREAD_WAIT_S)
        return ()

    def _labels_confirming(self) -> tuple[tuple[int, str], ...]:
        """读底部导航的标签，**空结果要重读几次再认**。

        同上一条。这里更要紧：读到空会让 `_reveal_ranking_label` 直接放弃，
        而放弃的理由「认不出这一屏」和「这一帧没读出来」是两回事。
        """
        for _attempt in range(READ_ATTEMPTS):
            labels = tuple(self.read_labels())
            if labels:
                return labels
            self.driver.wait(REREAD_WAIT_S)
        return ()

    # -- 分步慢拖 -----------------------------------------------------------

    def _slow_drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str) -> None:
        """按住 → 停一下 → 分步移动 → 停一下 → 松开。

        ⚠️ **一步到位的 `dragTo` 会被游戏面板当成点击**——同样的起止点，
        有时滚有时不滚（`tools.pirate_loop.slow_drag` 实机踩了好几次才看明白）。
        面板要收到连续的 mousemove 才认这是一次拖动。

        被当成点击的代价在这两处各不相同，但都不小：导航条那一拖按在
        `(1122, 862)`，那是两个导航项之间；榜单那一拖按在 `(960, 700)`，
        那是列表行上。

        `finally` 里松手不是仪式：`pyautogui` 的急停（鼠标甩到屏幕左上角）
        是从 `move_to` 里抛出来的，那时手指正按着——不松开的话，急停之后
        鼠标还是按下状态，用户接手时整个桌面都在拖东西。
        """
        self.driver.press(from_x, from_y, label=label)
        try:
            self.driver.wait(DRAG_PRESS_HOLD_S)
            for step in range(1, DRAG_STEPS + 1):
                self.driver.move_to(
                    from_x + (to_x - from_x) * step // DRAG_STEPS,
                    from_y + (to_y - from_y) * step // DRAG_STEPS,
                )
            self.driver.wait(DRAG_RELEASE_HOLD_S)
        finally:
            self.driver.release()


def merged_labels(entries: Sequence[tuple[int, str]]) -> list[tuple[int, str]]:
    """把靠得足够近的相邻词框合成一个标签，返回 `(中心 x, 完整标签)`。

    见 `ranking_ui.NAV_LABEL_WORD_GAP_PX`：tesseract 对中文按字切词，
    不合并的话「排名」永远只读到「排」或「名」，贴不回词表，于是永远找不到。

    中心 x 取整段的中点而不是首字——点在标签正中离相邻的导航项最远。

    与 `preset_picker.merged_names` 同形而不共用：那边的阈值
    （`PRESET_WORD_GAP_PX`）是在预设条上量的（字距 10 / 项距 237），
    这边是在导航条上量的（项距 80）。两个数各自会变，共用一个就会出现
    「改了预设条的容差，导航条跟着认错」。
    """
    ordered = sorted(entries)
    runs: list[list[tuple[int, str]]] = []
    for x, text in ordered:
        if runs and x - runs[-1][-1][0] <= NAV_LABEL_WORD_GAP_PX:
            runs[-1].append((x, text))
        else:
            runs.append([(x, text)])
    return [((run[0][0] + run[-1][0]) // 2, "".join(text for _x, text in run)) for run in runs]


def ranking_label_x(runs: Sequence[tuple[int, str]]) -> int | None:
    """这一屏里「排名」的中心 x，没有就 None。

    **贴回封闭词表**（`ranking_ui.NAV_LABELS`）而不是做子串判断：实机上
    `chi_sim` 把「攻击」读成过「政击」、把「派遣」读成过「派遗」，
    差一个字 `in` 就直接漏掉。而放宽成「含『名』就算」又会让别的项蒙混过关。
    `snap_to_vocabulary` 要求**唯一命中**，两个候选并列时判不出来而不是猜。

    ⚠️ 落在标签行 ROI 之外的 x 一律不当候选。眼下这道闸**打不着**——
    x 是从 ROI 裁出来的图上换算回来的，真实读数出不了界。留着它是因为
    ROI 和「用什么坐标去点」是两件各自会变的事：哪天有人改成整窗 OCR，
    这道闸就是唯一还站着的东西。**不要因为「测试构造不出真实场景」删掉它**
    （同 `preset_picker._clickable_hit` 里那条）。
    """
    for x, text in sorted(runs):
        snapped = snap_to_vocabulary(text, NAV_LABELS, max_distance=NAV_LABEL_MAX_DISTANCE)
        if snapped != RANKING_LABEL:
            continue
        if not NAV_LABEL_ROI[0] <= x <= NAV_LABEL_ROI[2]:
            continue
        return x
    return None


def nav_label_words(image: Any, ocr: Any) -> list[tuple[int, str]]:
    """从一张整窗截图里读出底部导航标签行的 `(中心 x, 文字)`。

    与 `preset_picker.name_words` 同形（那边找预设、这边找导航项），
    配方是实机 2026-08-14 用的那一套：`chi_sim`、`--psm 6`、3×。
    用词框而不是整行文本：要拿 x 去点。

    ⚠️ 只跑 `chi_sim` 不跑 `eng`：这五个标签全是中文，掺进 `eng` 只会多一份
    把「排」认成字母的机会。
    """
    crop = image.crop(NAV_LABEL_ROI).convert("L")
    grey = crop.resize(
        (crop.width * NAV_LABEL_UPSCALE, crop.height * NAV_LABEL_UPSCALE),
        _lanczos(),
    )
    data = ocr.image_to_data(grey, lang="chi_sim", config="--psm 6", output_type=ocr.Output.DICT)
    words: list[tuple[int, str]] = []
    for index, word in enumerate(data["text"]):
        text = word.strip()
        if not text:
            continue
        left = NAV_LABEL_ROI[0] + data["left"][index] // NAV_LABEL_UPSCALE
        width = data["width"][index] // NAV_LABEL_UPSCALE
        words.append((left + width // 2, text))
    return words


def _lanczos() -> Any:
    from PIL import Image

    return Image.Resampling.LANCZOS


__all__ = [
    "RankingDriver",
    "RankingNavigator",
    "RankingNotReached",
    "ScrollOutcome",
    "ScrollStep",
    "SpinResult",
    "merged_labels",
    "nav_label_words",
    "ranking_label_x",
]
