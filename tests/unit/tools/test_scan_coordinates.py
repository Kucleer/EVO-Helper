"""扫描器里不碰游戏也不碰数据库的那部分。"""

from __future__ import annotations

from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.game.system_navigator import SystemNavigator
from evo_helper.tools.scan_coordinates import scan_one
from evo_helper.vision.scan_reading import FREE_COORD_ROI, FREE_NAME_ROI


class RecordingDriver:
    """记下 goto 了几趟，并按脚本返回一屏一屏的读数。"""

    def __init__(self, reads: list[dict[tuple[int, int, int, int], str]]) -> None:
        self._reads = reads
        self.captures = 0

    def click(self, x: int, y: int, *, label: str = "") -> None:
        return None

    def type_number(self, value: int) -> None:
        return None

    def capture(self) -> Any:
        index = min(self.captures, len(self._reads) - 1)
        self.captures += 1
        return _Frame(self._reads[index])

    def wait(self, seconds: float) -> None:
        return None


class _Frame:
    def __init__(self, texts: dict[tuple[int, int, int, int], str]) -> None:
        self.texts = texts

    def crop(self, box: tuple[int, int, int, int]) -> tuple[dict[str, str], tuple[int, ...]]:
        return (self.texts, box)  # type: ignore[return-value]


def ocr(crop: Any, *, digits: bool, upscale: int, **_: object) -> str:
    texts, box = crop
    return texts.get(box, "")


def test_a_clean_read_does_not_retry() -> None:
    driver = RecordingDriver([{FREE_COORD_ROI: "[2:2:12]", FREE_NAME_ROI: "荒芜行星"}])
    result = scan_one(SystemNavigator(driver), ocr, Coordinate(2, 2, 12), debug_dir=None)
    assert result.confirmed
    assert driver.captures == 1


def test_a_short_read_is_retried_instead_of_silently_dropped() -> None:
    # 实测：2:2:11 被读成 [2:2:1]，相邻的两个 1 粘在一起少读一位。
    # 只读一次就放弃的话，这个坐标既不入库、游标又会被后面的成功坐标带过去。
    driver = RecordingDriver(
        [
            {FREE_COORD_ROI: "[2:2:1]"},
            {FREE_COORD_ROI: "[2:2:11]", FREE_NAME_ROI: "bot_2_2_11"},
        ]
    )
    result = scan_one(SystemNavigator(driver), ocr, Coordinate(2, 2, 11), debug_dir=None)
    assert result.confirmed
    assert result.panel.display_name == "bot_2_2_11"
    assert driver.captures == 2


def test_repeated_failures_stop_after_the_attempt_budget() -> None:
    driver = RecordingDriver([{FREE_COORD_ROI: "[9:9:9]"}])
    result = scan_one(
        SystemNavigator(driver), ocr, Coordinate(2, 2, 11), debug_dir=None, attempts=3
    )
    assert not result.confirmed
    assert driver.captures == 3


def test_a_failed_read_leaves_no_memory_behind() -> None:
    """读不出可能是因为根本没跳过去，所以重来时不能靠「记得刚才在哪」省字段。

    导航器的缓存里只放**回读确认过**的坐标（见 `SystemNavigator` 的类注释），
    而这一趟一次都没核对通过——所以走完之后它必须是空的，下一个坐标三个字段全重设。
    """
    driver = RecordingDriver([{FREE_COORD_ROI: "[9:9:9]"}])
    navigator = SystemNavigator(driver)
    navigator.current = Coordinate(2, 2, 10)
    scan_one(navigator, ocr, Coordinate(2, 2, 11), debug_dir=None, attempts=2)
    assert navigator.current is None


def test_a_confirmed_read_is_remembered_so_the_next_hop_is_cheap() -> None:
    """核对通过 = 导航栏的回读证据。不记下来，同一恒星系里每一位都要重设三个字段。"""
    driver = RecordingDriver([{FREE_COORD_ROI: "[2:2:12]", FREE_NAME_ROI: "荒芜行星"}])
    navigator = SystemNavigator(driver)
    scan_one(navigator, ocr, Coordinate(2, 2, 12), debug_dir=None)
    assert navigator.current == Coordinate(2, 2, 12)


def patch_db(monkeypatch, *, scanned, bots) -> None:
    from evo_helper.tools import scan_coordinates as tool

    monkeypatch.setattr(tool, "already_scanned", lambda _f: scanned)
    monkeypatch.setattr(tool, "systems_with_bot", lambda _f: bots)


def test_gap_fill_ignores_positions_the_scan_skipped_on_purpose(monkeypatch) -> None:
    """主循环找到 bot 就跳过本系剩余位；补缺口必须用同一条判据。

    否则那些位在补缺口眼里全是「缺口」，每跑一次重扫一遍，永远补不完。
    """
    from evo_helper.tools.scan_coordinates import missing_from_plan

    # 2:1 的 bot 在位 5 就找到了，位 6..20 是**故意**没扫的。
    patch_db(monkeypatch, scanned={(2, 1, 5)}, bots={(2, 1)})
    missing = list(missing_from_plan(object(), upto=Coordinate(2, 1, 20)))
    assert missing == []


def test_gap_fill_still_reports_a_system_with_no_bot_found_yet(monkeypatch) -> None:
    from evo_helper.tools.scan_coordinates import missing_from_plan

    patch_db(monkeypatch, scanned={(2, 1, 5), (2, 1, 6)}, bots=set())
    missing = list(missing_from_plan(object(), upto=Coordinate(2, 1, 8)))
    assert missing == [Coordinate(2, 1, 7), Coordinate(2, 1, 8)]


def test_turning_the_rule_off_reclaims_every_unscanned_position(monkeypatch) -> None:
    # --scan-full-systems 要能真的把这条假设撤销掉。
    from evo_helper.tools.scan_coordinates import missing_from_plan

    patch_db(monkeypatch, scanned={(2, 1, 5)}, bots={(2, 1)})
    missing = list(missing_from_plan(object(), upto=Coordinate(2, 1, 7), one_bot_per_system=False))
    assert missing == [Coordinate(2, 1, 6), Coordinate(2, 1, 7)]
