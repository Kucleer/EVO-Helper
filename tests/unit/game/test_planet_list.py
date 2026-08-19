"""切换星球：只点「前往此处」那一格，认不出就一次都不点。

行星列表浮层上每颗星球一行、每行**八个图标**：

    运输    部署    传送    前往此处      ← 名字行往下 60px
    转移    投送    保护    扩张          ← 名字行往下 130px

八个里只有右上那一个是我们要的。**「扩张」和「前往此处」在同一个 x 上**，
分开它俩的只有那 60 与 130 之差——所以这里的断言全部钉在**真实像素**上，
不写成 `名字行 y + PLANET_ICON_ROW_OFFSET_Y`：拿被守的常量当尺子，
常量被改坏时测试跟着改口，变异那一轮会是绿的。

⚠️ 全程不碰游戏：驱动是假的，OCR 读数是喂进来的清单。
"""

from __future__ import annotations

from evo_helper.domain.models import Coordinate
from evo_helper.game.planet_list import PlanetSwitcher, SwitchResult, coordinate_words

HOME = Coordinate(2, 137, 18)
SECOND = Coordinate(9, 250, 8)
THIRD = Coordinate(4, 96, 7)
MISSING = Coordinate(3, 300, 5)

#: 底部导航「行星」与浮层左上角的 ✕。这两个是**入口**，不算「点在那一排图标上」。
OPEN_LIST = (840, 862)
CLOSE_OVERLAY = (750, 71)
FLEET_PANEL = (920, 862)

#: 基准图（`var/logs/calib-切换星球-基准.png`）那一屏：三颗星球 + 一条行星大小噪声。
#:
#: y 用量出来的整数 190 / 420 / 650。实机 OCR 给出的词框中心是 191 / 421 / 651
#: （差 1px，见 `tests/unit/domain/test_planet_switch.py` 里那份真实读数）——
#: 那一像素点在哪个图标上都不影响，这里取整是为了让下面的期望像素读起来就是量出来的那几个。
#:
#: ⚠️ 坐标连方括号一起读回来，理由在 `domain.planet_switch._PLANET_ROW_RE`。
BASELINE = [(190, "[2:137:18]"), (211, "155223"), (420, "[9:250:8]"), (650, "[4:96:7]")]

#: 第 1/2/3 行的「前往此处」。**量出来的绝对坐标**，不是算出来的。
GOTO_ROW_1 = (1166, 250)
GOTO_ROW_2 = (1166, 480)
GOTO_ROW_3 = (1166, 710)

#: 同一列往下一排是「扩张」。点到它是把星球让出去这一类的真实操作。
EXPAND_ROW_1 = (1166, 320)


class _List:
    """一列**上下都能滚**的行星清单。`screens` 从上到下排开，两端夹住。

    读一屏只交出**那一屏**的词框——测试里唯一的位置来源。实现要是从别处变出
    坐标来（缓存上一屏、按行号外推），下面的断言就接不上。

    `starts_at` 就是「打开列表时它停在第几屏」。⚠️ **实机上这个值不是 0**：
    2026-08-19 关掉再打开，列表停在上一趟拖到的位置上——这正是要修的缺陷。
    """

    def __init__(self, screens: list[list[tuple[int, str]]], *, starts_at: int = 0) -> None:
        self.screens = screens
        self.at = min(starts_at, len(screens) - 1)
        self.reads = 0

    def read(self) -> list[tuple[int, str]]:
        self.reads += 1
        return list(self.screens[self.at])

    def scroll(self) -> None:
        self.at = min(len(self.screens) - 1, self.at + 1)

    def scroll_back(self) -> None:
        self.at = max(0, self.at - 1)


class _FlakyList(_List):
    """指定次数把已有行读成空，模拟列表刚展开时的单帧 OCR 失手。"""

    def __init__(self, screens: list[list[tuple[int, str]]], *, blank_times: int) -> None:
        super().__init__(screens)
        self._blank_times = blank_times

    def read(self) -> list[tuple[int, str]]:
        self.reads += 1
        if self._blank_times > 0:
            self._blank_times -= 1
            return []
        return list(self.screens[self.at])


class _Driver:
    def __init__(self, planets: _List) -> None:
        self._planets = planets
        self.clicks: list[tuple[int, int, str]] = []
        self.drags: list[tuple[int, int, int, str]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))

    @property
    def opens(self) -> int:
        """点开底部导航「行星」的次数——也就是「读了几遍列表」。"""
        return self.points.count(OPEN_LIST)

    @property
    def closes(self) -> int:
        return self.points.count(CLOSE_OVERLAY)

    def drag_vertical(self, x: int, from_y: int, to_y: int, *, label: str = "") -> None:
        """往上拖（松手点更高）= 内容上移 = 往下翻；往下拖 = 往回翻。

        方向由起止点自己决定，不看 `label`：实现改了标签而方向搞反时，
        这里必须跟着错，否则「回顶」那几条用例会在一个方向反了的实现上照样绿。
        """
        self.drags.append((x, from_y, to_y, label))
        if to_y < from_y:
            self._planets.scroll()
        else:
            self._planets.scroll_back()

    @property
    def back_drags(self) -> list[tuple[int, int, int, str]]:
        """往回拖（回顶）的那几下。"""
        return [drag for drag in self.drags if drag[2] > drag[1]]

    @property
    def forward_drags(self) -> list[tuple[int, int, int, str]]:
        """往下翻找目标的那几下。"""
        return [drag for drag in self.drags if drag[2] < drag[1]]

    def wait(self, seconds: float) -> None:
        del seconds

    @property
    def points(self) -> list[tuple[int, int]]:
        return [(x, y) for x, y, _label in self.clicks]

    @property
    def in_panel(self) -> list[tuple[int, int]]:
        """落在浮层内容区上的点击——也就是**可能点到那八个图标**的那些。

        底部导航（行星 / 舰队）与左上角 ✕ 是入口和出口，不算：它们的位置是写死的
        常量，与「认到了哪一行」无关。剩下的每一个点都是这一层自己算出来的坐标，
        而算错的代价就是那七个图标之一。
        """
        entries = (OPEN_LIST, CLOSE_OVERLAY, FLEET_PANEL)
        return [point for point in self.points if point not in entries]


class _CoveredDriver(_Driver):
    """点开「行星」满 `opens_needed` 次之前，画面上都盖着**别的**浮层。

    盖着时点 `NAV_PLANET` 那一下落在浮层上、什么都没打开，于是坐标列读什么都是空
    ——这正是 2026-08-17 实机那一屏：「太空舱」面板（材料/星云/加速器/资源/舰长/
    行星工具）压住了整条底部导航栏连同行星列表。

    `opens_needed` 就是「要重开几次列表才露出来」，用它可以把重试次数钉死：
    2 = 关一轮浮层就好了，3 = 关一轮还不够（正确实现必须在这里放弃）。
    """

    def __init__(self, planets: _List, *, opens_needed: int) -> None:
        super().__init__(planets)
        self._opens_needed = opens_needed

    def read(self) -> list[tuple[int, str]]:
        return [] if self.opens < self._opens_needed else self._planets.read()


def _switcher(
    screens: list[list[tuple[int, str]]],
    *,
    origin_reads: str = "2:137:18",
    dry_run: bool = False,
    starts_at: int = 0,
    evidence: list[tuple[str, dict[str, object]]] | None = None,
    said: list[str] | None = None,
) -> tuple[PlanetSwitcher, _Driver, _List]:
    planets = _List(screens, starts_at=starts_at)
    driver = _Driver(planets)
    recorded = evidence if evidence is not None else []
    switcher = PlanetSwitcher(
        driver=driver,  # type: ignore[arg-type]
        read_rows=planets.read,
        read_origin=lambda: origin_reads,
        say=said.append if said is not None else (lambda _message: None),
        record_evidence=lambda message, payload: recorded.append((message, payload)),
        dry_run=dry_run,
    )
    return switcher, driver, planets


def _covered_switcher(
    screens: list[list[tuple[int, str]]],
    *,
    opens_needed: int,
    origin_reads: str = "2:137:18",
    evidence: list[tuple[str, dict[str, object]]] | None = None,
    sees_close_button: bool = True,
) -> tuple[PlanetSwitcher, _CoveredDriver]:
    driver = _CoveredDriver(_List(screens), opens_needed=opens_needed)
    recorded = evidence if evidence is not None else []

    def sees() -> bool:
        """盖着的那层浮层上有个 ✕；**点一下它就没了**，所以之后就认不出了。

        实机由 `tools.pirate_loop` 接到 `game.overlay.look_at_close_button`，
        判据本身钉在 `tests/unit/game/test_overlay_close_button.py` 上。
        """
        if not sees_close_button:
            return False
        return not any(label == "关闭面板" for _x, _y, label in driver.clicks)

    switcher = PlanetSwitcher(
        driver=driver,  # type: ignore[arg-type]
        read_rows=driver.read,
        read_origin=lambda: origin_reads,
        say=lambda _message: None,
        record_evidence=lambda message, payload: recorded.append((message, payload)),
        see_close_button=sees,
    )
    return switcher, driver


class TestClickingOnlyTheGoToColumn:
    def test_the_first_row_is_clicked_at_the_pixel_it_was_calibrated_at(self) -> None:
        switcher, driver, _planets = _switcher([BASELINE], origin_reads="2:137:18")

        assert switcher.switch_to(HOME) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_1]

    def test_the_second_row_is_clicked_one_row_lower(self) -> None:
        switcher, driver, _planets = _switcher([BASELINE], origin_reads="9:250:8")

        assert switcher.switch_to(SECOND) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_2]

    def test_the_third_row_too(self) -> None:
        switcher, driver, _planets = _switcher([BASELINE], origin_reads="4:96:7")

        assert switcher.switch_to(THIRD) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_3]

    def test_the_click_never_lands_on_the_second_icon_row(self) -> None:
        """**同一个 x 上，往下 70px 坐着「扩张」。**

        这条单独钉一遍，因为它是这一层最容易悄悄错掉的地方：图标排的偏移量是个
        魔数，改大一档不会有任何报错，只会在实机上把星球扩张出去。
        """
        switcher, driver, _planets = _switcher([BASELINE])

        switcher.switch_to(HOME)

        assert EXPAND_ROW_1 not in driver.points

    def test_the_row_is_read_again_immediately_before_the_click(self) -> None:
        """与 `game.action_guard` 的「点击前重新观察」同形：点之前再认一次那一行。"""
        switcher, _driver, planets = _switcher([BASELINE])

        switcher.switch_to(HOME)

        assert planets.reads >= 2, "点之前必须再读一次这一屏"

    def test_a_row_that_moved_between_the_two_reads_is_not_clicked(self) -> None:
        """复核读到的 y 变了 = 列表还在动。这时点下去点的是「刚才那个位置」。"""
        planets = _List([BASELINE])
        driver = _Driver(planets)
        answers = [BASELINE, [(310, "[2:137:18]")], BASELINE, [(310, "[2:137:18]")]]

        def read() -> list[tuple[int, str]]:
            return answers.pop(0) if answers else []

        switcher = PlanetSwitcher(
            driver=driver,  # type: ignore[arg-type]
            read_rows=read,
            read_origin=lambda: "2:137:18",
            say=lambda _message: None,
        )

        assert switcher.switch_to(HOME) is SwitchResult.NOT_FOUND
        assert driver.in_panel == []


class TestRefusingToGuess:
    def test_a_transiently_blank_list_is_read_again_before_giving_up(self) -> None:
        """空读不是「没有星球」：否则两处出发点都会在预检时被安全跳过。"""
        planets = _FlakyList([BASELINE], blank_times=2)
        driver = _Driver(planets)
        switcher = PlanetSwitcher(
            driver=driver,  # type: ignore[arg-type]
            read_rows=planets.read,
            read_origin=lambda: "2:137:18",
            say=lambda _message: None,
        )

        assert switcher.switch_to(HOME) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_1]
        assert planets.reads >= 4  # 两次空读 + 找到 + 点击前回读

    def test_an_unreadable_screen_costs_zero_clicks_in_the_panel(self) -> None:
        """一行都认不出时**一次都不点**。

        绝不按行号盲点：那一排里转移/投送/保护/扩张点错任何一个都是真实操作，
        而「第一行就是主星」这种假设一旦不成立，代价就是其中之一。

        ⚠️ 结局是 `UNREADABLE` 而不是 `NOT_FOUND`：这一屏的噪声（行星大小 `155223`、
        图标漏出来的 `5`）一条坐标行都没解析出来，也就**说不出列表里有没有主星**。
        """
        switcher, driver, _planets = _switcher([[(211, "155223"), (353, "5")]])

        assert switcher.switch_to(HOME) is SwitchResult.UNREADABLE
        assert driver.in_panel == []

    def test_a_planet_that_is_nowhere_in_the_list_costs_zero_clicks(self) -> None:
        switcher, driver, _planets = _switcher([BASELINE])

        assert switcher.switch_to(MISSING) is SwitchResult.NOT_FOUND
        assert driver.in_panel == []

    def test_the_overlay_is_closed_again_when_nothing_was_found(self) -> None:
        """找不到也要把浮层关掉：留着它，下游一切照坐标点下去的动作都压在浮层底下。"""
        switcher, driver, _planets = _switcher([BASELINE])

        switcher.switch_to(MISSING)

        assert CLOSE_OVERLAY in driver.points


class TestClosingOverlaysWhenTheListReadsBlank:
    """实机故障（2026-08-17 11:20–11:40）：一发都没派，连着多轮。

    日志每一轮都是同一段：

        行星列表坐标 OCR 全空；tesseract='C:\\Program Files\\Tesseract-OCR\\tesseract.exe'
        行星列表上找不到 4:277:15；逐屏读到的是 [[]]；什么都不点
        切不到出发星球 4:277:15（not_found）；这一轮一发都不派

    用户的现场截图给出了答案：游戏停在「太空舱」面板上，它把整条底部导航栏连同
    行星列表一起盖住了。所以 `[[]]` 不是 OCR 读错，是**那个位置上根本没有行星
    列表**——点「行星」那一下落在浮层上，什么都没打开。而关浮层的机制早就有
    （`game.overlay`，坐标扫描链路一直在用），只是从没接到这条链路上：
    攻击链路里没有任何一步会去关那个面板。
    """

    def test_a_blank_list_is_read_again_after_the_overlays_are_closed(self) -> None:
        """本文件的重点：读空先当「有浮层盖着」处理，而不是直接判 `NOT_FOUND`。"""
        switcher, driver = _covered_switcher([BASELINE], opens_needed=2)

        assert switcher.switch_to(HOME) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_1]

    def test_the_retry_happens_exactly_once(self) -> None:
        """**关完重读还是空，就按 `UNREADABLE` 收场。**

        这里的列表要开到第三次才露出来，而正确实现只会开两次（原本一次 + 重试
        一次）。做成循环重试的话它就能等到第三次、返回 `SWITCHED`——那正是这条
        用例要挡住的：无限重试会把整轮卡死在一个关不掉的面板上，比跳过还糟。
        """
        switcher, driver = _covered_switcher([BASELINE], opens_needed=3)

        assert switcher.switch_to(HOME) is SwitchResult.UNREADABLE
        assert driver.opens == 2, "开一次、重试一次，不许再多"

    def test_a_readable_list_without_the_target_never_closes_an_overlay(self) -> None:
        """⚠️ **这道界限是整个改动的要害。**

        读到了内容却没有目标那一行，是**另一回事**：多半是任务把出发星球配错了
        （`Outcome.busy_is_permanent` 正是照这一条分流的）。把它也当成「被浮层
        盖住」，代价是每次配错坐标都要先朝 (750, 71) 盲点几下——而星球地表上
        那个位置本仓从没标定过。

        这一轮唯一允许的那一下 ✕ 是收尾的「关掉列表」。
        """
        switcher, driver, _planets = _switcher([BASELINE])

        assert switcher.switch_to(MISSING) is SwitchResult.NOT_FOUND
        assert driver.closes == 1, "只许有收尾那一下，不许有关浮层那一串"
        assert driver.opens == 1, "读得到内容就不该重开列表"

    def test_a_readable_list_with_the_target_never_closes_an_overlay_either(self) -> None:
        """稳态是绝大多数情况：认到了那一行，就不该有关浮层那一串。

        这一轮唯一的那一下 ✕ 是回读之后关派遣面板（点完「前往此处」那一步
        **故意不 `_close()`**，见 `switch_to` 里那段注释）。
        """
        switcher, driver, _planets = _switcher([BASELINE])

        assert switcher.switch_to(HOME) is SwitchResult.SWITCHED
        assert driver.closes == 1, "只许有回读收尾那一下"

    def test_the_recovery_leaves_evidence_that_survives_the_machine(self) -> None:
        """今天这次排障靠的正是日志里那句「逐屏读到的是 `[[]]`」。

        这一支把那句话补全成一条完整的因果：读空 → 疑似浮层 → 关了几下 →
        重读读到了什么。实机由 `tools.pirate_loop` 接到 `system_log`（还捎一张
        缩略图），跨机就查得到，不必再等用户手工截图。
        """
        evidence: list[tuple[str, dict[str, object]]] = []
        switcher, _driver = _covered_switcher([BASELINE], opens_needed=2, evidence=evidence)

        switcher.switch_to(HOME)

        assert len(evidence) == 1
        message, payload = evidence[0]
        assert "浮层" in message
        assert payload["screens_before"] == [[]], "读空那一遍原样留下"
        assert payload["screens_after"] == [["[2:137:18]", "[9:250:8]", "[4:96:7]"]]
        assert payload["close_clicks"] == 1, "浮层关掉之后 ✕ 就没了，不该继续点"
        assert payload["close_button_recognised"] is True
        assert payload["recovered"] is True
        assert payload["target"] == "2:137:18"

    def test_a_recovery_that_did_not_work_says_so(self) -> None:
        evidence: list[tuple[str, dict[str, object]]] = []
        switcher, _driver = _covered_switcher([BASELINE], opens_needed=3, evidence=evidence)

        switcher.switch_to(HOME)

        assert len(evidence) == 1
        assert evidence[0][1]["recovered"] is False
        assert evidence[0][1]["screens_after"] == [[]]

    def test_nothing_is_recorded_when_the_list_read_fine(self) -> None:
        """稳态不许往库里写东西——这一支每出现一次就意味着一轮没派。"""
        evidence: list[tuple[str, dict[str, object]]] = []
        switcher, _driver = _covered_switcher([BASELINE], opens_needed=1, evidence=evidence)

        assert switcher.switch_to(HOME) is SwitchResult.SWITCHED
        assert evidence == []

    def test_an_unrecognised_close_button_costs_zero_clicks(self) -> None:
        """⚠️ **实机 2026-08-18 10:04 / 10:05：那两次画面上是军力排行榜面板。**

        原先这一支不看那儿是什么，朝 (750, 71) 盲点 4 下——4 下全落进了榜里。
        用户口径：「点 4 下关闭，应校验按钮形态，不然就会点到排行榜中去」。

        认不出 ✕ 就一下都不点，也不重开列表（重开只会再读一张同样的画面）。
        这一轮唯一允许的那一下 ✕ 是收尾的「关掉列表」。
        """
        switcher, driver = _covered_switcher([BASELINE], opens_needed=2, sees_close_button=False)

        assert switcher.switch_to(HOME) is SwitchResult.UNREADABLE
        assert driver.closes == 1, "只许有收尾那一下，不许有关浮层那一串"
        assert driver.opens == 1, "认不出就别重开列表"

    def test_the_unrecognised_close_button_is_written_down(self) -> None:
        """「认不出」必须留痕，否则库里只会看到一轮莫名其妙的没派。"""
        evidence: list[tuple[str, dict[str, object]]] = []
        switcher, _driver = _covered_switcher(
            [BASELINE], opens_needed=2, evidence=evidence, sees_close_button=False
        )

        switcher.switch_to(HOME)

        assert len(evidence) == 1
        message, payload = evidence[0]
        assert payload["close_button_recognised"] is False
        assert payload["close_clicks"] == 0
        assert "认不出" in message


class TestBlankIsNotTheSameAsMissing:
    """⚠️ **实机 2026-08-18 10:04:07 与 10:05:10：日志对用户说了一句假话。**

        切不到出发星球 4:277:15（not_found）；这一轮一发都不派
        这颗星球不在你的行星列表里；请核对任务配的出发星球 4:277:15

    4:277:15 就是用户的主星。真实原因是那两轮列表一行都没读出来。而调用方照
    `NOT_FOUND` 把 `Outcome.busy_is_permanent` 置了真——退出码 1、计入连续失败、
    走向自动停用，连着两轮 exit=1。

    判据看的是**逐屏读到的行**，不是「找没找到目标」。
    """

    def test_a_list_that_never_read_a_single_row_is_unreadable(self) -> None:
        switcher, driver = _covered_switcher([BASELINE], opens_needed=9)

        assert switcher.switch_to(HOME) is SwitchResult.UNREADABLE
        assert driver.in_panel == []

    def test_a_list_that_read_rows_without_the_target_is_still_not_found(self) -> None:
        """⚠️ **这一半不许跟着一起放宽。**

        列表读通了、里面确实没有这颗星球——那是真的配错了坐标，不会自己好，
        必须让连续失败计数看见它。把它也说成「读不出来」，就等于给自己开了一个
        永不停用的静默死循环。
        """
        switcher, driver, _planets = _switcher([BASELINE])

        assert switcher.switch_to(MISSING) is SwitchResult.NOT_FOUND
        assert driver.in_panel == []

    def test_rows_read_on_the_retry_alone_are_enough_to_be_not_found(self) -> None:
        """重读那一遍读到了内容 = 列表翻通了，照 `NOT_FOUND` 走。"""
        switcher, _driver = _covered_switcher([BASELINE], opens_needed=2, origin_reads="")

        assert switcher.switch_to(MISSING) is SwitchResult.NOT_FOUND

    def test_the_unreadable_verdict_never_blames_the_configuration(self) -> None:
        """读不出来时**不许**说「这颗星球不在你的行星列表里」。"""
        said: list[str] = []
        driver = _CoveredDriver(_List([BASELINE]), opens_needed=9)
        switcher = PlanetSwitcher(
            driver=driver,  # type: ignore[arg-type]
            read_rows=driver.read,
            read_origin=lambda: "",
            say=said.append,
        )

        assert switcher.switch_to(HOME) is SwitchResult.UNREADABLE
        spoken = "\n".join(said)
        assert "找不到" not in spoken
        assert "一行都没读出来" in spoken


def test_coordinate_words_turns_an_ocr_timeout_into_a_safe_empty_read() -> None:
    """实机 tesseract 卡住时不能让调度任务永远占用一条航线。"""
    from PIL import Image

    class _TimedOutOcr:
        class Output:
            DICT = "dict"

        @staticmethod
        def image_to_data(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("Tesseract process timeout")

    assert (
        coordinate_words(
            Image.new("RGB", (1920, 1080)),
            _TimedOutOcr(),
            upscale=2,
            resample="lanczos",
            whitelist="0123456789:",
        )
        == []
    )


class TestDraggingThroughTheList:
    def test_a_planet_further_down_is_found_after_dragging(self) -> None:
        screens = [
            [(190, "[2:137:18]"), (420, "[9:250:8]")],
            [(190, "[4:96:7]"), (420, "[3:300:5]")],
        ]
        switcher, driver, _planets = _switcher(screens, origin_reads="3:300:5")

        assert switcher.switch_to(MISSING) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_2], "点的是**当屏**那一行，不是拖之前的位置"

    def test_the_finger_presses_down_on_a_name_row_read_from_this_screen(self) -> None:
        """按下点的 y 必须跟着**当前这一屏识别出来的行**走。

        写死一个绝对值不行：横向中点只在星球名那一行是空白，往下 60px 就是图标上排，
        同一个 x 上坐着「部署」。列表拖过之后行会移位——而按下再拖起来，
        游戏可能当成点击。

        这里第一屏的行**不在**基准图那三个高度上（300/530/760），所以只要实现
        偷偷用了 190/420/650 里的任何一个，这条就红。
        """
        screens = [
            [(300, "[2:137:18]"), (530, "[9:250:8]"), (760, "[4:96:7]")],
            [(190, "[3:300:5]")],
        ]
        switcher, driver, _planets = _switcher(screens, origin_reads="3:300:5")

        switcher.switch_to(MISSING)

        assert [(x, from_y) for x, from_y, _to, _label in driver.forward_drags] == [(961, 760)]

    def test_the_drag_back_to_the_top_presses_on_this_screens_rows_too(self) -> None:
        """回顶那一下的起止点同样只能来自当前这一屏：按下 `rows[0]`、松手 `rows[-1]`。

        这一屏的行在 300/530/760，都不在基准图那三个高度上——实现要是写死了
        190/420/650 里的任何一个，这条就红。写死的代价与往下翻那一下完全一样：
        横向中点只在星球名那一行是空白，往下 60px 就是图标上排。
        """
        screens = [
            [(300, "[2:137:18]"), (530, "[9:250:8]"), (760, "[4:96:7]")],
            [(190, "[3:300:5]")],
        ]
        switcher, driver, _planets = _switcher(screens, origin_reads="3:300:5")

        switcher.switch_to(MISSING)

        assert [(x, a, b) for x, a, b, _label in driver.back_drags] == [(961, 300, 760)]

    def test_two_identical_screens_end_the_search_without_a_click(self) -> None:
        """「这一屏读到的和上一屏一样」= 到底了。仍没找到就什么都不点。"""
        switcher, driver, _planets = _switcher([BASELINE, BASELINE])

        assert switcher.switch_to(MISSING) is SwitchResult.NOT_FOUND
        assert driver.in_panel == []
        assert len(driver.forward_drags) == 1, "确认到底之后不该再往下拖"

    def test_the_dragging_is_bounded(self) -> None:
        """每一屏都不一样时也必须停下来，否则一条读坏的清单能把整轮拖没。"""
        screens = [[(190 + step, f"[5:{step + 1}:1]")] for step in range(40)]
        switcher, driver, _planets = _switcher(screens)

        assert switcher.switch_to(MISSING) is SwitchResult.NOT_FOUND
        assert len(driver.drags) <= 8
        assert driver.in_panel == []


#: 一列六颗星球、每屏三行，从顶到底四屏——用户 2026-08-19 的清单形状。
#: 顶上那两颗正是他配的两个出发星球。
SIX_PLANETS = [
    [(190, "[4:277:15]"), (420, "[9:250:8]"), (650, "[4:96:7]")],
    [(190, "[9:250:8]"), (420, "[4:96:7]"), (650, "[7:228:15]")],
    [(190, "[4:96:7]"), (420, "[7:228:15]"), (650, "[1:55:6]")],
    [(190, "[7:228:15]"), (420, "[1:55:6]"), (650, "[9:411:17]")],
]
TOP_PLANET = Coordinate(4, 277, 15)
BOTTOM_PLANET = Coordinate(9, 411, 17)


class TestGettingBackToTheTopBeforeSearching:
    """⚠️ **实机 2026-08-19：7 次切换失败里至少 2 次倒在这上面。**

    生产 `system_log` 三条并排就能看出来：

        13:48:41  第1屏 ['4:277:15','9:250:88','4:96:7'] … 一路拖到
                  第4屏 ['7:228:15','1:55:6','9:411:17'] （判到底）
        13:49:11  找 4:277:15；第1屏读到的就是 ['7:228:15','1:55:6','9:411:17']
        13:49:40  找 9:250:8； 第1屏读到的还是那三颗

    **关掉再打开，列表停在上一趟拖到的位置上。** 而找目标只会往下翻，排在顶部的
    那两颗出发星球于是永远够不着。这个缺陷会**自我延续**：拖到底一次之后，
    后面每一趟都从底部开始，全部失败。
    """

    def test_a_planet_at_the_top_is_found_even_when_the_list_reopens_at_the_bottom(self) -> None:
        """本文件这一节的重点：先回顶，再往下找。

        列表停在最后一屏（上一趟拖到的地方），而目标排在第一屏。不回顶的实现
        会在第一屏就判 `list_exhausted`、返回 `NOT_FOUND`——那正是 13:49:11 的样子。
        """
        switcher, driver, _planets = _switcher(SIX_PLANETS, starts_at=3, origin_reads="4:277:15")

        assert switcher.switch_to(TOP_PLANET) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_1]

    def test_the_second_starting_planet_is_reachable_again_too(self) -> None:
        """13:49:40 那一趟找的是 9:250:8，它在第一屏的第二行。"""
        switcher, driver, _planets = _switcher(SIX_PLANETS, starts_at=3, origin_reads="9:250:8")

        assert switcher.switch_to(SECOND) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_2]

    def test_a_planet_at_the_bottom_is_still_reachable_after_going_back_to_the_top(self) -> None:
        """回顶不许把往下翻那条路弄丢：排在最底下那颗照样要找得到。"""
        switcher, driver, _planets = _switcher(SIX_PLANETS, starts_at=3, origin_reads="9:411:17")

        assert switcher.switch_to(BOTTOM_PLANET) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_3]

    def test_the_drags_back_stop_as_soon_as_the_list_stops_moving(self) -> None:
        """停止判据是「拖了一下坐标还是那几个」，不是「拖够几次」。

        从最后一屏回到第一屏要 3 下，再多 1 下确认拖不动了——**一共 4 下**。
        写死次数的实现（比如照信箱那次的老样子拖 3 下）会少一下或多好几下，
        而多拖一下就是一秒多，少拖一下就是这次的缺陷复发。
        """
        switcher, driver, _planets = _switcher(SIX_PLANETS, starts_at=3, origin_reads="4:277:15")

        switcher.switch_to(TOP_PLANET)

        assert len(driver.back_drags) == 4

    def test_a_list_that_never_settles_still_stops(self) -> None:
        """⚠️ **上界不许缺。** 每一屏都不一样时也得停下来。

        这一列每拖一下都换一批坐标（实机上「拖不动了」判不出来就是这个样子），
        没有上界的实现会在这里永远拖下去，把整个攻击进程挂在开工阶段。
        """
        never_settles = [
            [(190, f"[5:{step + 1}:1]"), (420, f"[5:{step + 2}:1]"), (650, f"[5:{step + 3}:1]")]
            for step in range(60)
        ]
        switcher, driver, _planets = _switcher(never_settles, starts_at=59)

        assert switcher.switch_to(MISSING) is SwitchResult.NOT_FOUND
        assert len(driver.back_drags) <= 6, "回顶必须有上界"

    def test_walking_into_the_bound_says_so_instead_of_claiming_the_top(self) -> None:
        """⚠️ **走满上限时不许说「已经在顶部」。**

        信箱那条链路上就是这句话说过谎：走满上限的 17 趟里用户当场核对过，
        进邮箱本来就在顶部，而日志却断言「看到的不是最新的几封」。仓库口径写在
        CLAUDE.md 上——**日志说假话比不说更糟**。到没到顶不知道就说不知道。

        这一句同时是「上界该不该做成可配置」的凭据：库里出现它，才说明
        `PLANET_LIST_TO_TOP_MAX_DRAGS` 真的不够用了。
        """
        said: list[str] = []
        evidence: list[tuple[str, dict[str, object]]] = []
        never_settles = [
            [(190, f"[5:{step + 1}:1]"), (420, f"[5:{step + 2}:1]"), (650, f"[5:{step + 3}:1]")]
            for step in range(60)
        ]
        switcher, _driver, _planets = _switcher(
            never_settles, starts_at=59, said=said, evidence=evidence
        )

        switcher.switch_to(MISSING)

        spoken = "\n".join(said)
        assert "到没到顶判不出来" in spoken
        assert "到顶" not in spoken.replace("到没到顶判不出来", "")
        assert len(evidence) == 1
        assert evidence[0][1]["max_drags"] == 6

    def test_two_rows_are_needed_to_press_and_release_on(self) -> None:
        """一屏只认出一行时**一下都不拖**：按下和松手都必须落在识别出来的名字行上。

        认不出行就没有安全的按下点——那个 x 在星球名那一行是空白，往下 60px
        就是图标上排。这时安静退出，后面照原样走到「读不出 / 找不到」。
        """
        switcher, driver, _planets = _switcher([[(190, "[2:137:18]")]])

        assert switcher.switch_to(HOME) is SwitchResult.SWITCHED
        assert driver.back_drags == []

    def test_a_blank_screen_never_counts_as_the_top(self) -> None:
        """⚠️ **整屏读空不算到顶。**

        空的时候两屏的坐标序列都是 `[]`，「和上一屏一样」照样成立——就此停手
        等于把一次识别失败当成了一个位置事实。这里前两帧读空（列表刚展开时的
        真实抖动），正确实现要读到内容之后再判，于是照样回得了顶、切得过去。
        """
        planets = _FlakyList(SIX_PLANETS, blank_times=2)
        planets.at = 3
        driver = _Driver(planets)
        switcher = PlanetSwitcher(
            driver=driver,  # type: ignore[arg-type]
            read_rows=planets.read,
            read_origin=lambda: "4:277:15",
            say=lambda _message: None,
        )

        assert switcher.switch_to(TOP_PLANET) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_1]

    def test_the_verdict_still_separates_unreadable_from_missing(self) -> None:
        """⚠️ **回顶读到的行不许算进「读得出来吗」那道闸。**

        `screens`（往下找那几屏）是 `UNREADABLE` 与 `NOT_FOUND` 之间唯一的证据。
        把回顶那几屏掺进去，「回顶读得到、找的时候一行都读不出」就会被判成
        `NOT_FOUND`，于是日志指着用户的配置说一句它并不知道的话，调用方还会照
        「不会自己好」把这一轮计成永久失败（`Outcome.busy_is_permanent`）。

        这里回顶时读得到内容，`_locate` 一开始就再也读不出——结局必须是
        `UNREADABLE`。
        """
        answers = [BASELINE, BASELINE]

        def read() -> list[tuple[int, str]]:
            return answers.pop(0) if answers else []

        driver = _Driver(_List([BASELINE]))
        said: list[str] = []
        switcher = PlanetSwitcher(
            driver=driver,  # type: ignore[arg-type]
            read_rows=read,
            read_origin=lambda: "",
            say=said.append,
        )

        assert switcher.switch_to(HOME) is SwitchResult.UNREADABLE
        assert driver.in_panel == []
        assert "找不到" not in "\n".join(said)


class TestReadingAMisreadCoordinate:
    """⚠️ **实机 2026-08-19：`9:250:8` 读成 `9:250:88`，方括号被顶成了数字。**

    量法与凭据在 `domain.planet_switch._PLANET_ROW_RE`。这一节守的是**后果**：
    读错的那一行必须走「什么都不点」，不能变成一次真实点击。
    """

    def test_a_row_whose_bracket_became_a_digit_is_never_clicked(self) -> None:
        """`9:250:88` 这一行认不出来 → 一下都不点。

        方向只能是这一个：读不出的代价是这一轮不派，而读错的代价是**点在另一颗
        星球那一行上**——而那一行右边同一个 x 上还坐着「扩张」。
        """
        misread = [(190, "[4:277:15]"), (420, "9:250:88"), (650, "[4:96:7]")]
        switcher, driver, _planets = _switcher([misread], origin_reads="9:250:8")

        assert switcher.switch_to(SECOND) is SwitchResult.NOT_FOUND
        assert driver.in_panel == []

    def test_the_rows_that_did_read_cleanly_are_still_usable(self) -> None:
        """一行读坏了不该拖累同一屏上读干净的那几行。"""
        misread = [(190, "[4:277:15]"), (420, "9:250:88"), (650, "[4:96:7]")]
        switcher, driver, _planets = _switcher([misread], origin_reads="4:277:15")

        assert switcher.switch_to(TOP_PLANET) is SwitchResult.SWITCHED
        assert driver.in_panel == [GOTO_ROW_1]

    def test_a_misread_screen_is_not_confused_with_a_covered_one(self) -> None:
        """整屏都读坏 = 一行都没成行 = `UNREADABLE`，**不是** `NOT_FOUND`。

        读坏和被盖住在这一层是同一件事：都说不出列表里有什么。而「翻通了、
        里面确实没有这颗」才是 `NOT_FOUND`，那一档要指着用户的配置说话。
        """
        all_misread = [(190, "14:277:15"), (420, "9:250:88"), (650, "[2:137:1")]
        switcher, driver, _planets = _switcher([all_misread])

        assert switcher.switch_to(HOME) is SwitchResult.UNREADABLE
        assert driver.in_panel == []


class TestConfirmingAfterTheClick:
    def test_an_origin_line_that_does_not_match_reports_unconfirmed(self) -> None:
        """**绝不「点了就当切成了」。** 回读对不上就报 `UNCONFIRMED`，调用方本轮不派。"""
        switcher, _driver, _planets = _switcher([BASELINE], origin_reads="4:96:7")

        assert switcher.switch_to(HOME) is SwitchResult.UNCONFIRMED

    def test_an_unreadable_origin_line_also_reports_unconfirmed(self) -> None:
        switcher, _driver, _planets = _switcher([BASELINE], origin_reads="")

        assert switcher.switch_to(HOME) is SwitchResult.UNCONFIRMED

    def test_the_readback_opens_the_fleet_panel_and_closes_it_again(self) -> None:
        """回读只读不派：开舰队面板、读一行、点 ✕。绿✓（1156, 763）一步都不靠近。"""
        switcher, driver, _planets = _switcher([BASELINE])

        switcher.switch_to(HOME)

        assert FLEET_PANEL in driver.points
        assert (1156, 763) not in driver.points


class TestDryRun:
    def test_dry_run_never_clicks_go_to_here(self) -> None:
        """放开真实切换之前给人看的那一档：认到哪一行说出来，但一下都不点。"""
        switcher, driver, _planets = _switcher([BASELINE], dry_run=True)

        assert switcher.switch_to(HOME) is SwitchResult.DRY_RUN
        assert driver.in_panel == []

    def test_dry_run_says_which_pixel_and_why(self) -> None:
        said: list[str] = []
        planets = _List([BASELINE])
        driver = _Driver(planets)
        switcher = PlanetSwitcher(
            driver=driver,  # type: ignore[arg-type]
            read_rows=planets.read,
            read_origin=lambda: "",
            say=said.append,
            dry_run=True,
        )

        switcher.switch_to(SECOND)

        spoken = "\n".join(said)
        assert "(1166, 480)" in spoken
        assert "[9:250:8]" in spoken

    def test_dry_run_does_not_open_the_fleet_panel_either(self) -> None:
        """回读要开派遣面板。演一遍时连它也不开——那一档是给人看的，不该改画面状态。"""
        switcher, driver, _planets = _switcher([BASELINE], dry_run=True)

        switcher.switch_to(HOME)

        assert FLEET_PANEL not in driver.points


def test_the_calibrated_geometry_is_the_one_measured_on_the_baseline_screenshot() -> None:
    """把那两个魔数钉在 `var/logs/calib-切换星球-基准.png` 上量出来的绝对像素上。

    基准图（client 1920×917）：坐标行在 y=190 / 420 / 650，「前往此处」在
    (1166, 250) / (1166, 480) / (1166, 710)。上面那些用例都从这份几何出发，
    这一条是那份几何自己的凭据——改坏它，实机上点的就是别的图标。
    """
    from evo_helper.game.pirate_ui import PLANET_GOTO_COLUMN_X, PLANET_ICON_ROW_OFFSET_Y

    assert PLANET_GOTO_COLUMN_X == 1166
    assert [y + PLANET_ICON_ROW_OFFSET_Y for y in (190, 420, 650)] == [250, 480, 710]
