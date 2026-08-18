"""盲拖余量告急时的那条 WARNING，以及实测样本那句话的往返。

⚠️ **自动标定有一个自己看不见的盲点。** 拖过头的表现是「第一屏检测就看到 bot」，
而那和「刚好卡在 bot 起点上」在数据上一模一样——两种都记成
`scrolled == blind_scrolls`。真拖过头时，被跳过去的那一批 bot 不会报错、
不会少一条日志，只是**采回来的数静悄悄少一截**。

所以余量一旦被吃掉就得主动喊一声，而且要喊在 `WARNING` 级别上：控制台的日志页
可以按级别筛，混在一轮几千条 INFO 里等于没报。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from evo_helper.domain.ranking import bot_area_reached_message, bot_area_scrolls
from evo_helper.game.ranking_ui import BLIND_SCROLL_MARGIN
from evo_helper.infrastructure.system_log import (
    SystemLogContext,
    SystemLogRecord,
    SystemLogSink,
    current_system_log_sink,
    install_system_log_sink,
    shutdown_system_log_sink,
)
from evo_helper.tools.ranking_scan import report_bot_area_reached


class Collector:
    def __init__(self) -> None:
        self.records: list[SystemLogRecord] = []

    def __call__(self, batch) -> None:  # type: ignore[no-untyped-def]
        self.records.extend(batch)


@pytest.fixture
def collector() -> Iterator[Collector]:
    sink_collector = Collector()
    install_system_log_sink(
        SystemLogSink(sink_collector, flush_interval_s=0.01), context=SystemLogContext()
    )
    try:
        yield sink_collector
    finally:
        shutdown_system_log_sink()


def _flush() -> None:
    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)


# -- 实测样本那句话 ------------------------------------------------------------


def test_the_measurement_sentence_round_trips() -> None:
    """写出去的那句话必须读得回来。

    它同时是一句给人看的日志和自动标定**唯一**的样本来源。措辞一改，库里全部
    历史样本一次性作废，而作废之后自动标定会静悄悄退回写死的默认值——
    页面上、日志里都看不出任何异常。
    """
    assert bot_area_scrolls(bot_area_reached_message(77)) == 77
    assert bot_area_reached_message(77) == "翻了 77 屏到达 bot 区"


@pytest.mark.parametrize(
    "line",
    [
        "翻满 130 屏（盲拖 70 + 检测预算 60）仍没见到 bot；本轮到此为止",
        "盲拖 40 屏（那一段必定还是真人），开始检测 bot",
        "上一趟翻了 77 屏到达 bot 区",
        "翻了 屏到达 bot 区",
    ],
)
def test_other_log_lines_are_not_mistaken_for_measurements(line: str) -> None:
    """只认整句。把复述或别的句子当成样本，标定就建在假数上了。"""
    assert bot_area_scrolls(line) is None


# -- 余量告急 ------------------------------------------------------------------


def test_the_measurement_is_always_recorded(collector: Collector) -> None:
    """不管余量够不够，样本那一句都得留下——它是标定唯一的输入。"""
    report_bot_area_reached(72, blind_scrolls=52)
    _flush()

    assert [entry.message for entry in collector.records] == [bot_area_reached_message(72)]


def test_a_comfortable_margin_says_nothing_more(collector: Collector) -> None:
    """余量够就别多喊——天天喊的告警等于没有告警。"""
    report_bot_area_reached(72, blind_scrolls=62 - BLIND_SCROLL_MARGIN)
    _flush()

    assert [entry.level for entry in collector.records] == ["INFO"]


def test_a_thin_margin_raises_a_warning(collector: Collector) -> None:
    """余量被吃掉就喊，而且必须是 `WARNING`：日志页按级别筛得到。"""
    report_bot_area_reached(66, blind_scrolls=62)
    _flush()

    assert [entry.level for entry in collector.records] == ["INFO", "WARNING"]
    message = collector.records[-1].message
    assert "66" in message and "62" in message, "告警里要说清实测几屏、盲拖几屏"
    assert "余量只剩 4 屏" in message


def test_landing_exactly_on_the_first_bot_screen_raises_a_warning(collector: Collector) -> None:
    """余量 0 是最危险的一种：它和「已经拖过头了」长得一模一样。"""
    report_bot_area_reached(62, blind_scrolls=62)
    _flush()

    assert [entry.level for entry in collector.records] == ["INFO", "WARNING"]
    assert "余量只剩 0 屏" in collector.records[-1].message
