"""认出「到达时撞保护期」的那一刻**必须留痕**，而且痕迹要够定位。

CLAUDE.md 的判据不是「有没有打日志」，而是**出事时能不能只靠库里的日志定位**。
这一条要回答的四个问题：哪个坐标、邮件写的什么时刻、结的是哪一发、白占了多少
航线时间。少任何一个，这条日志就退回成一句「发生了某件事」——而这个缺陷之所以
藏到 2026-08-21 才被发现，正是因为它在账上和「战报还没回来」长得一模一样
（生产库近 3 天提到「保护状态」的日志：0 条）。

⚠️ **这一条不限流。** 限流针对的是「每 tick 可能触发」的那些；这一条一封信只走
一次，而且每一次都值钱——它记的是白占掉的一整趟往返。

⚠️ **认不出是哪一发时也要留痕，并且要说清「没结掉」。** 含糊一句「已处理」的话，
「结掉了」与「那一发仍然永久挂在未读回上」在库里就分不开——日志说假话比不说更糟。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.infrastructure.system_log import (
    SystemLogContext,
    SystemLogRecord,
    SystemLogSink,
    current_system_log_sink,
    install_system_log_sink,
    shutdown_system_log_sink,
)
from evo_helper.storage.repository import ProtectionBounceOutcome
from evo_helper.tools.pirate_loop import MailRow, PirateLoop
from evo_helper.vision.parsers import ReportKind

TARGET = Coordinate(4, 321, 9)
ORIGIN = Coordinate(2, 137, 18)
DISPATCHED_AT = datetime(2026, 8, 20, 13, 27, 26, tzinfo=UTC)
ONE_WAY_SECONDS = 3724
#: 页眉上写的那个时刻（= 抵达那一刻，实测比 `派出 + 单程` 晚 1 秒）。
MAIL_AT = datetime(2026, 8, 20, 14, 29, 32, tzinfo=UTC)

BODY = "[4:321:9]（bot_4_321_9's Planet）处于保护状态，我方舰队已返航。"
HEADER = "发件人: 系统\n主题: 舰队返航\n20/08/2026 14:29:32"

ROW = MailRow(
    index=3,
    subject=BODY,
    raw_time_text="20/08/2026 14:29:32",
    reported_at_utc=MAIL_AT,
    kind=ReportKind.PROTECTION_BOUNCE,
)


class _Mail:
    def __init__(self, body: str = BODY) -> None:
        self._body = body

    def report_header(self) -> str:
        return HEADER

    def security_message(self) -> str:
        return self._body


class _Repository:
    def __init__(self, outcome: ProtectionBounceOutcome) -> None:
        self._outcome = outcome
        self.calls: list[tuple[Coordinate, datetime, str | None]] = []

    def record_protection_bounce(
        self, target: Coordinate, *, mail_at_utc: datetime, raw_time_text: str | None = None
    ) -> ProtectionBounceOutcome:
        self.calls.append((target, mail_at_utc, raw_time_text))
        return self._outcome


def _closed(dispatch_id: UUID) -> ProtectionBounceOutcome:
    return ProtectionBounceOutcome(
        target=TARGET,
        mail_at_utc=MAIL_AT,
        protection_noted=True,
        report_id=uuid4(),
        dispatch_id=dispatch_id,
        origin=ORIGIN,
        dispatched_at_utc=DISPATCHED_AT,
        line_free_at_utc=DISPATCHED_AT + timedelta(seconds=2 * ONE_WAY_SECONDS),
        flight_seconds=ONE_WAY_SECONDS,
        unmatched_candidates=1,
        ambiguous_arrivals=1,
        military_score=20960.0,
    )


class _Collector:
    def __init__(self) -> None:
        self.records: list[SystemLogRecord] = []

    def __call__(self, batch: Any) -> None:
        self.records.extend(batch)


@pytest.fixture
def collector() -> Any:
    sink_collector = _Collector()
    install_system_log_sink(
        SystemLogSink(sink_collector, flush_interval_s=0.01), context=SystemLogContext()
    )
    try:
        yield sink_collector
    finally:
        shutdown_system_log_sink()


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    from evo_helper.tools import pirate_loop

    monkeypatch.setattr(pirate_loop, "say", lambda _line: None)


def _flush() -> None:
    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)


def _loop(repository: _Repository) -> Any:
    loop = PirateLoop.__new__(PirateLoop)
    loop._ensure_run = lambda: (repository, None)  # type: ignore[method-assign]
    return loop


def test_the_log_names_the_coordinate_mail_time_dispatch_and_wasted_line(
    collector: Any,
) -> None:
    dispatch_id = uuid4()
    repository = _Repository(_closed(dispatch_id))

    _loop(repository)._ingest_protection_bounce(ROW, _Mail())
    _flush()

    (record,) = collector.records
    assert record.level == "INFO"
    assert record.source == "tools.protection_bounce"
    assert "到达时撞保护期" in record.message
    assert "4:321:9" in record.message
    # 白占的是**一整趟往返**（124 分钟），不是单程的 62 分。
    assert "124 分钟" in record.message
    assert "单程 62 分" in record.message

    payload = json.loads(record.payload_json)
    assert payload["coordinate"] == "4:321:9"
    assert payload["mail_at_utc"] == MAIL_AT.isoformat()
    assert payload["dispatch_id"] == str(dispatch_id)
    assert payload["wasted_line_minutes"] == 124.1
    assert payload["military_score"] == 20960.0
    assert payload["dispatch_closed"] is True


def test_the_protection_moment_written_is_the_mail_time(collector: Any) -> None:
    """⚠️ 传给仓储的是**邮件时刻**，不是我们翻到它的时刻。"""
    repository = _Repository(_closed(uuid4()))

    _loop(repository)._ingest_protection_bounce(ROW, _Mail())

    (target, mail_at, raw_time_text) = repository.calls[0]
    assert target == TARGET
    assert mail_at == MAIL_AT
    assert raw_time_text == "20/08/2026 14:29:32"


def test_a_dispatch_that_could_not_be_identified_is_said_so_out_loud(
    collector: Any,
) -> None:
    """没结掉就要**明说没结掉**，并且升到 WARNING——那一发仍算未读回。"""
    repository = _Repository(
        ProtectionBounceOutcome(
            target=TARGET,
            mail_at_utc=MAIL_AT,
            protection_noted=True,
            unmatched_candidates=2,
            ambiguous_arrivals=2,
        )
    )

    _loop(repository)._ingest_protection_bounce(ROW, _Mail())
    _flush()

    (record,) = collector.records
    assert record.level == "WARNING"
    # ⚠️ 带告警记号的原句，**不许**被改写成一句读起来像成功的话（「已处理（…）」）。
    # 「结掉了」与「那一发仍然永久挂在未读回上」在库里必须一眼分得开。
    assert "⚠️ 认不出这封信结的是哪一发" in record.message
    assert "仍算未读回" in record.message
    assert "已处理" not in record.message
    assert json.loads(record.payload_json)["dispatch_closed"] is False
    assert json.loads(record.payload_json)["dispatch_id"] is None
    # 飞行时长无从谈起时**不许报一个 0**——那会读成「一分钟都没浪费」。
    assert json.loads(record.payload_json)["wasted_line_minutes"] is None
    assert "白占航线不明" in record.message


def test_a_missing_bot_target_row_is_said_so_out_loud(collector: Any) -> None:
    """保护期没记上 = 下一轮那个坐标还会被挑中，再白占一趟。"""
    repository = _Repository(
        ProtectionBounceOutcome(
            target=TARGET,
            mail_at_utc=MAIL_AT,
            protection_noted=False,
            report_id=uuid4(),
            dispatch_id=uuid4(),
            flight_seconds=ONE_WAY_SECONDS,
            dispatched_at_utc=DISPATCHED_AT,
        )
    )

    _loop(repository)._ingest_protection_bounce(ROW, _Mail())
    _flush()

    (record,) = collector.records
    assert record.level == "WARNING"
    assert "保护期没记上" in record.message
    assert json.loads(record.payload_json)["protection_noted"] is False


def test_the_mailbox_scan_routes_this_kind_to_its_own_reader(collector: Any) -> None:
    """⚠️ 接线本身要有人守：分流漏了它，这封信会被当成战报去读。

    症状不报错——读不出来、放过、下一趟再来一遍，永远读不进去。三条翻信箱的路
    （收侦察报告、开工对账、手动补录）共用 `_ingest_non_report_mail` 这一处分流，
    所以守住它就等于三条一起守住。
    """
    from evo_helper.tools.pirate_loop import NON_REPORT_MAIL_KINDS

    repository = _Repository(_closed(uuid4()))

    # 归它管：就地处理掉，**不**往下走战报解析器。
    assert _loop(repository)._ingest_non_report_mail(ROW, _Mail()) is True
    assert repository.calls != []
    # `wanted` 与分流读的是同一份名单，两处不许各写一份。
    assert ReportKind.PROTECTION_BOUNCE in NON_REPORT_MAIL_KINDS


def test_a_real_battle_report_row_is_left_to_the_report_reader(collector: Any) -> None:
    """反过来也要守：攻击战报**不许**被这条分流截走。"""
    repository = _Repository(_closed(uuid4()))
    attack_row = MailRow(
        index=0,
        subject="主题: 攻击报告",
        raw_time_text="20/08/2026 14:29:32",
        reported_at_utc=MAIL_AT,
        kind=ReportKind.ATTACK,
    )

    assert _loop(repository)._ingest_non_report_mail(attack_row, _Mail()) is False
    assert repository.calls == []


def test_an_unreadable_mail_leaves_a_trace_too(collector: Any) -> None:
    """读不齐也要留痕：不然「没这封信」和「读坏了」在库里分不开。"""
    repository = _Repository(_closed(uuid4()))

    _loop(repository)._ingest_protection_bounce(ROW, _Mail(body="[4:321:9] 保护"))
    _flush()

    (record,) = collector.records
    assert record.level == "WARNING"
    assert "读不齐" in record.message
    assert repository.calls == []
