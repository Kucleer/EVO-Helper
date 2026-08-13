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
from evo_helper.game.planet_list import PlanetSwitcher, SwitchResult

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
BASELINE = [(190, "2:137:18"), (211, "155223"), (420, "9:250:8"), (650, "4:96:7")]

#: 第 1/2/3 行的「前往此处」。**量出来的绝对坐标**，不是算出来的。
GOTO_ROW_1 = (1166, 250)
GOTO_ROW_2 = (1166, 480)
GOTO_ROW_3 = (1166, 710)

#: 同一列往下一排是「扩张」。点到它是把星球让出去这一类的真实操作。
EXPAND_ROW_1 = (1166, 320)


class _List:
    """一列可以往下滚的行星清单。`screens` 从上到下排开，两端夹住。

    读一屏只交出**那一屏**的词框——测试里唯一的位置来源。实现要是从别处变出
    坐标来（缓存上一屏、按行号外推），下面的断言就接不上。
    """

    def __init__(self, screens: list[list[tuple[int, str]]]) -> None:
        self.screens = screens
        self.at = 0
        self.reads = 0

    def read(self) -> list[tuple[int, str]]:
        self.reads += 1
        return list(self.screens[self.at])

    def scroll(self) -> None:
        self.at = min(len(self.screens) - 1, self.at + 1)


class _Driver:
    def __init__(self, planets: _List) -> None:
        self._planets = planets
        self.clicks: list[tuple[int, int, str]] = []
        self.drags: list[tuple[int, int, int, str]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y, label))

    def drag_vertical(self, x: int, from_y: int, to_y: int, *, label: str = "") -> None:
        self.drags.append((x, from_y, to_y, label))
        self._planets.scroll()

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


def _switcher(
    screens: list[list[tuple[int, str]]],
    *,
    origin_reads: str = "2:137:18",
    dry_run: bool = False,
) -> tuple[PlanetSwitcher, _Driver, _List]:
    planets = _List(screens)
    driver = _Driver(planets)
    switcher = PlanetSwitcher(
        driver=driver,  # type: ignore[arg-type]
        read_rows=planets.read,
        read_origin=lambda: origin_reads,
        say=lambda _message: None,
        dry_run=dry_run,
    )
    return switcher, driver, planets


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
        answers = [BASELINE, [(310, "2:137:18")], BASELINE, [(310, "2:137:18")]]

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
    def test_an_unreadable_screen_costs_zero_clicks_in_the_panel(self) -> None:
        """一行都认不出时**一次都不点**。

        绝不按行号盲点：那一排里转移/投送/保护/扩张点错任何一个都是真实操作，
        而「第一行就是主星」这种假设一旦不成立，代价就是其中之一。
        """
        switcher, driver, _planets = _switcher([[(211, "155223"), (353, "5")]])

        assert switcher.switch_to(HOME) is SwitchResult.NOT_FOUND
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


class TestDraggingThroughTheList:
    def test_a_planet_further_down_is_found_after_dragging(self) -> None:
        screens = [
            [(190, "2:137:18"), (420, "9:250:8")],
            [(190, "4:96:7"), (420, "3:300:5")],
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
            [(300, "2:137:18"), (530, "9:250:8"), (760, "4:96:7")],
            [(190, "3:300:5")],
        ]
        switcher, driver, _planets = _switcher(screens, origin_reads="3:300:5")

        switcher.switch_to(MISSING)

        assert [(x, from_y) for x, from_y, _to, _label in driver.drags] == [(961, 760)]

    def test_two_identical_screens_end_the_search_without_a_click(self) -> None:
        """「这一屏读到的和上一屏一样」= 到底了。仍没找到就什么都不点。"""
        switcher, driver, _planets = _switcher([BASELINE, BASELINE])

        assert switcher.switch_to(MISSING) is SwitchResult.NOT_FOUND
        assert driver.in_panel == []
        assert len(driver.drags) == 1, "确认到底之后不该再拖"

    def test_the_dragging_is_bounded(self) -> None:
        """每一屏都不一样时也必须停下来，否则一条读坏的清单能把整轮拖没。"""
        screens = [[(190 + step, f"5:{step + 1}:1")] for step in range(40)]
        switcher, driver, _planets = _switcher(screens)

        assert switcher.switch_to(MISSING) is SwitchResult.NOT_FOUND
        assert len(driver.drags) <= 8
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
        assert "9:250:8" in spoken

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
