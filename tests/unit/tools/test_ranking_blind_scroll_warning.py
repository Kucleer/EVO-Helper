"""盲滚余量告急时的那条 WARNING，以及实测样本那句话的往返。

⚠️ **自动标定有一个自己看不见的盲点。** 滚过头的表现是「第一屏检测就看到 bot」，
而那和「刚好停在 bot 起点上」在数据上一模一样——两种都记成
`rows == blind_rows`。真滚过头时，被跳过去的那一批 bot 不会报错、
不会少一条日志，只是**采回来的数静悄悄少一截**。

所以余量一旦被吃掉就得主动喊一声，而且要喊在 `WARNING` 级别上：控制台的日志页
可以按级别筛，混在一轮几千条 INFO 里等于没报。

⚠️ **口径是「行」，不是「屏」**（2026-08-22）。滚轮那一段根本没有「屏」这个概念，
而按行比余量就少一次 `ROWS_PER_SCROLL` 换算——少一次换算就少一处能悄悄错量纲的
地方。库里那一年屏版历史归 `bot_area_scrolls` 读，**不参与行版标定**。
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from evo_helper.domain.ranking import (
    bot_area_reached_message,
    bot_area_reached_rows_message,
    bot_area_rows,
)
from evo_helper.game.ranking_ui import BLIND_SCROLL_MARGIN_ROWS
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

    它同时是一句给人看的日志和自动标定**唯一**的样本来源。措辞一改，已经攒下的
    样本一次性作废，而作废之后自动标定会静悄悄退回写死的默认值——
    页面上、日志里都看不出任何异常。
    """
    assert bot_area_rows(bot_area_reached_rows_message(566)) == 566
    assert bot_area_reached_rows_message(566) == "翻了 566 行到达 bot 区"


@pytest.mark.parametrize(
    "line",
    [
        "盲滚 700 行之后又翻满 60 屏检测预算仍没见到 bot；本轮到此为止",
        "盲滚 700 行（实走约 700 行，那一段必定还是真人），开始检测 bot",
        "上一趟翻了 566 行到达 bot 区",
        "翻了 行到达 bot 区",
    ],
)
def test_other_log_lines_are_not_mistaken_for_measurements(line: str) -> None:
    """只认整句。把复述或别的句子当成样本，标定就建在假数上了。"""
    assert bot_area_rows(line) is None


def test_the_old_screen_flavoured_sentence_is_never_read_as_rows() -> None:
    """⚠️ **库里存着一整年「翻了 N 屏到达 bot 区」，一条都不许被当成行。**

    78 屏 ≈ 647 行。当成 78 行的话自标定会给出一个荒谬的小值——小值本身是安全的
    （只是白花检测段那 4.6 秒/屏），但那是撞上的而不是算出来的。
    """
    assert bot_area_rows(bot_area_reached_message(78)) is None


# -- 余量告急 ------------------------------------------------------------------


def test_the_measurement_is_always_recorded(collector: Collector) -> None:
    """不管余量够不够，样本那一句都得留下——它是标定唯一的输入。"""
    report_bot_area_reached(600, blind_rows=430)
    _flush()

    assert [entry.message for entry in collector.records] == [bot_area_reached_rows_message(600)]


def test_a_comfortable_margin_says_nothing_more(collector: Collector) -> None:
    """余量够就别多喊——天天喊的告警等于没有告警。"""
    report_bot_area_reached(600, blind_rows=600 - BLIND_SCROLL_MARGIN_ROWS)
    _flush()

    assert [entry.level for entry in collector.records] == ["INFO"]


def test_a_thin_margin_raises_a_warning(collector: Collector) -> None:
    """余量被吃掉就喊，而且必须是 `WARNING`：日志页按级别筛得到。"""
    report_bot_area_reached(600, blind_rows=596)
    _flush()

    assert [entry.level for entry in collector.records] == ["INFO", "WARNING"]
    message = collector.records[-1].message
    assert "600" in message and "596" in message, "告警里要说清实测几行、盲滚几行"
    assert "余量只剩 4 行" in message


def test_landing_exactly_on_the_first_bot_row_raises_a_warning(collector: Collector) -> None:
    """余量 0 是最危险的一种：它和「已经滚过头了」长得一模一样。"""
    report_bot_area_reached(596, blind_rows=596)
    _flush()

    assert [entry.level for entry in collector.records] == ["INFO", "WARNING"]
    assert "余量只剩 0 行" in collector.records[-1].message


def test_the_warning_never_mentions_a_bot_start_boundary(collector: Collector) -> None:
    """⚠️ **不许把 `FIRST_BOT_RANK`(587) 拿来当边界。**

    用户口径（2026-08-22）：那段「bot 起点」是**玩家改名伪装**出来的，不是真 bot
    （判据只看名字前缀，改名的真人一样命中），真 bot 区在更后面。拿一个被伪装
    污染的边界报警比不报更坏——它会天天喊，而天天喊的告警等于没有告警。

    所以余量 0 的判据只比两个实测量，587 一次都不该出现在这条链路上。
    """
    report_bot_area_reached(596, blind_rows=596)
    _flush()

    assert "587" not in "\n".join(entry.message for entry in collector.records)
