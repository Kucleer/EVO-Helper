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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

from evo_helper.domain.text import snap_to_vocabulary
from evo_helper.game.ranking_ui import (
    DRAG_PRESS_HOLD_S,
    DRAG_RELEASE_HOLD_S,
    DRAG_STEPS,
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
    RANKING_CLOSE,
    RANKING_LABEL,
    READ_ATTEMPTS,
    REREAD_WAIT_S,
    SCROLL_FROM_Y,
    SCROLL_SETTLE_WAIT_S,
    SCROLL_TO_Y,
    SCROLL_X,
    TAB_SWITCH_WAIT_S,
)


class RankingDriver(Protocol):
    """本层需要的最小操作面。真实实现驱动鼠标，测试里换成假的。

    `press` / `move_to` / `release` 是**分步慢拖**要的三个原语：
    一步式的 `dragTo` 会被游戏面板当成点击（`tools.pirate_loop.slow_drag`
    的注释里记着这条实测），而分步这件事必须发生在 `game` 层，
    否则就得反过来 import `tools`。
    """

    def click(self, x: int, y: int, *, label: str = ...) -> None: ...

    def press(self, x: int, y: int, *, label: str = ...) -> None: ...

    def move_to(self, x: int, y: int) -> None: ...

    def release(self) -> None: ...

    def wait(self, seconds: float) -> None: ...


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


@dataclass
class RankingNavigator[RowT]:
    """把画面带到军事排行榜上，并一屏一屏往下滚。

    三个回调都由调用方注入，**这一层不认识 OCR，测试里也就不需要假图片**
    （同 `game.planet_list.PlanetSwitcher`）：

    - `read_labels`：底部导航标签行的 `(中心 x, 文字)` 词框。
      那个 x 就是待会儿要点的地方——所以必须是**这一屏**读出来的。
    - `read_rows`：当前这一屏的榜单行，**认不出的丢掉**（见模块头的契约）。
      行是什么类型由调用方定，实机上是 `domain.ranking.RankingRow`。
    ⚠️ **没有「这一屏是不是军事榜」这个回调**，因为那个判据不存在——见
    `_switch_to_military`。想看哪个榜就点哪个页签。
    """

    driver: RankingDriver
    read_labels: Callable[[], Sequence[tuple[int, str]]]
    read_rows: Callable[[], Sequence[RowT]]
    say: Callable[[str], None] = print

    # -- 进榜单 -------------------------------------------------------------

    def open_military_ranking(self) -> tuple[RowT, ...]:
        """走到军事排行榜，返回**回读确认过**的第一屏。

        每一步都先认出这一屏再点下一下；认不出就抛 `RankingNotReached`，不盲点。
        """
        x = self._reveal_ranking_label()
        self.driver.click(x, NAV_BAR_Y, label=RANKING_LABEL)
        self.driver.wait(PANEL_OPEN_WAIT_S)
        rows = self._rows_confirming()
        if not rows:
            raise RankingNotReached(
                f"点完「{RANKING_LABEL}」之后一行榜单都读不出来："
                "面板没开出来，或者已经不在榜单页上了（比如断线）"
            )
        del rows  # 打开时那一屏是经济榜，没有用；真正要的是切过去之后那一屏
        return self._switch_to_military()

    def _switch_to_military(self) -> tuple[RowT, ...]:
        """点一次「军事评分」，回读确认还在榜单上。

        ⚠️ **面板打开时停在「经济评分」**（用户 2026-08-14 实测），所以这一下是必须的，
        不是可选的兜底。

        ⚠️ **页签是幂等的按钮，不是开关**（用户实测：已经在军事榜上再点一次不会切回去）。
        所以用户的口径是「你不用管现在是什么，你需要看什么，就点什么切换」——
        先点再说比先判断再点简单，而且**不需要一个单屏判据**。

        ⚠️ **那个单屏判据本来就不存在。** 这里原先有个 `on_military_board` 回调，
        实机上用的判据是「读到任何非零分数就说明在军事榜」。2026-08-14 当场证伪：
        两个页签的榜首十三行都是真人、分数都在 404.17M 这个量级，非零判据两边都成立。
        「经济榜 bot 全是 0」只对第 639 名之后成立，而看到那一段要先滚六十屏。

        回读仍然要做，但它回答的是**另一个**问题：「我还在不在排行榜面板上」
        （读到 0 行 = 断线或面板没开），不是「我在哪个页签上」。
        """
        self.driver.click(*MILITARY_TAB, label="军事评分")
        self.driver.wait(TAB_SWITCH_WAIT_S)
        rows = self._rows_confirming()
        if not rows:
            raise RankingNotReached(
                "点完「军事评分」之后一行榜单都读不出来；"
                "画面已经不是排行榜面板了（比如断线），不再点下去"
            )
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

    # -- 收尾 ---------------------------------------------------------------

    def close(self) -> bool:
        """关掉面板，并把底部导航条**还原回左段**；还原确认了才返回 True。

        ⚠️ 还原不是洁癖。拖之前 `pirate_ui.NAV_PLANET`(840, 862) 是「行星」，
        拖之后同一个像素上坐着的是**「太空舱」**（拖完中心在 830，差 10px），
        而 `pirate_ui` 里写着那个东西点开是材料仓库、还会把整条导航条盖住。
        把条留在右段就交回控制权，等于给下一条链路埋了一颗雷。
        同一条规矩 `preset_picker.pick` 的收尾里已经写过一遍。

        判据是回读：还原之后标签行**读得出东西**，而且里面**没有「排名」**。
        读不出来就如实返回 False——「读到 0 个标签」永远不算证据（模块头第 1 条）。
        """
        self.driver.click(*RANKING_CLOSE, label="关闭排行榜")
        self.driver.wait(NAV_DRAG_WAIT_S)
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
    "merged_labels",
    "nav_label_words",
    "ranking_label_x",
]
