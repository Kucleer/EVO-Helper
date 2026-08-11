"""侦察报告读完就扔的那条路补上了：读通一份就落库一份。

原先 `collect_scout_reports()` 把报告读成 `PirateScoutReading` 交给
`_decide_and_attack()` 用一次就丢，进程一退什么都不剩。链路因此每一轮都当作
没侦察过——2026-08-11 实机 31 发派遣里 25 发是重复侦察，同样四颗星球
（2:137:1–4）被来回打了 6 轮。

这里守三件事：

1. **每一份读通的都进库**，包括「不在本轮目标里」的那些——它们恰恰是上几轮
   侦察留下的证据，重复侦察这件事就靠它们才看得见。
2. **返回值一个字没变**：仍旧是 `dict[Coordinate, reading]`，仍旧只含本轮目标。
   入库是加出来的一步，不是改出来的。
3. **同一份翻到两次只有一行**（走的是仓库那条真去重，不是这里另写一套）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from evo_helper.domain.models import Coordinate
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.tools import pirate_loop
from evo_helper.tools.pirate_loop import LoopOptions, MailRow, PirateLoop
from evo_helper.vision.parsers import ReportKind

ORIGIN = Coordinate(2, 137, 18)
FULL = {"深空吞噬者": 2, "噬能截击者": 4, "钛能守卫者": 4, "收割者": 0}


class _Screens:
    """一封侦察报告的两屏读数。与 `tests/unit/vision/test_scout_reports.py` 同形。"""

    def __init__(
        self, *, target: Coordinate, at: str, counts: dict[str, int] | None = None
    ) -> None:
        self._target = target
        self._at = at
        self._counts = FULL if counts is None else counts

    def report_header(self) -> str:
        return f"发件人: Aries [HQ]        {self._at}\n主题: 侦察报告"

    def scout_intro_texts(self) -> list[str]:
        target = f"{self._target.galaxy}:{self._target.system}:{self._target.position}"
        return [f"2:137:18 {target} 3\n:\n"]

    def named_counts(self, wanted, band, top, bottom, *, count_band=None) -> dict[str, int]:  # type: ignore[no-untyped-def]
        assert count_band is not None, "数量列必须写死传入，不能现场量"
        return {name: value for name, value in self._counts.items() if name in wanted}


def _mail(index: int, at: str) -> MailRow:
    return MailRow(
        index=index,
        subject="侦察报告",
        raw_time_text=at,
        reported_at_utc=None,
        kind=ReportKind.SCOUT,
    )


def _loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mails: list[_Screens]) -> Any:
    """一个只剩「读报告 + 写库」的 `PirateLoop`：不开窗、不点鼠标、不进真信箱。

    库是每个测试自己一个临时文件——**绝不碰生产库**。
    """
    engine = create_database_engine(f"sqlite:///{tmp_path / 'loop.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=(), scout=False, attack=True)
    loop._started_at = datetime(2026, 8, 11, tzinfo=UTC)
    loop._driver = object()
    # `_ensure_run` 两个都非空就直接返回，不会去开 `Settings().database_url`。
    loop._repository = SqlAlchemyRepository(factory)
    loop._run_id = uuid4()

    monkeypatch.setattr(pirate_loop, "slow_drag", lambda *args, **kwargs: None)
    current: dict[str, Any] = {}
    loop._report_screens = lambda: current["screens"]

    scans: list[dict[str, Any]] = []

    def scan(**kwargs: Any) -> None:
        scans.append(kwargs)
        for index, screens in enumerate(mails):
            current["screens"] = screens
            if kwargs["visit"](_mail(index, "读过了"), screens):
                break

    loop._scan_mail_rows = scan
    loop.scans = scans
    loop.factory = factory
    return loop


def _stored_targets(loop: Any) -> list[tuple[int, int, int]]:
    with loop.factory() as session:
        rows = session.scalars(select(orm.ScoutReportRow)).all()
        return sorted((row.target_galaxy, row.target_system, row.target_position) for row in rows)


def test_every_readable_report_lands_in_the_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本文件的重点。四封读通，四行入库。"""
    targets = [Coordinate(2, 137, position) for position in (1, 2, 3, 4)]
    loop = _loop(
        tmp_path,
        monkeypatch,
        [
            _Screens(target=target, at=f"11/08/2026 03:3{index}:11")
            for index, target in enumerate(targets)
        ],
    )

    loop.collect_scout_reports(targets)

    assert _stored_targets(loop) == [(2, 137, 1), (2, 137, 2), (2, 137, 3), (2, 137, 4)]


def test_a_report_outside_this_round_is_stored_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """「不是我这轮要的」不等于「不用记」——重复侦察这件事就靠这些行才看得见。"""
    loop = _loop(
        tmp_path,
        monkeypatch,
        [
            _Screens(target=Coordinate(2, 137, 3), at="11/08/2026 02:46:00"),
            _Screens(target=Coordinate(2, 137, 1), at="11/08/2026 03:32:11"),
        ],
    )

    found = loop.collect_scout_reports([Coordinate(2, 137, 1)])

    # 上一轮那份（2:137:3）不在本轮目标里，但一样入了库。
    assert _stored_targets(loop) == [(2, 137, 1), (2, 137, 3)]
    # 而返回值仍旧只含本轮目标——既有行为一个字没变。
    assert list(found) == [Coordinate(2, 137, 1)]


def test_the_returned_readings_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_decide_and_attack` 拿到的还是那个 `PirateScoutReading`，判定照旧现算。"""
    target = Coordinate(2, 137, 4)
    loop = _loop(tmp_path, monkeypatch, [_Screens(target=target, at="11/08/2026 03:32:11")])

    found = loop.collect_scout_reports([target])

    reading = found[target]
    assert reading.target == target
    assert reading.origin == ORIGIN
    assert reading.reported_at_utc == datetime(2026, 8, 11, 3, 32, 11, tzinfo=UTC)
    assert reading.trigger_ships == FULL
    assert reading.verdict == "ATTACK"


def test_the_same_report_seen_twice_is_one_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """活链路每一轮都会翻同样那几行；去重口径是目标 + 报告时间。"""
    target = Coordinate(2, 137, 4)
    loop = _loop(
        tmp_path,
        monkeypatch,
        [
            _Screens(target=target, at="11/08/2026 03:32:11"),
            _Screens(target=target, at="11/08/2026 03:32:11"),
        ],
    )

    loop.collect_scout_reports([Coordinate(2, 137, 9)])

    with loop.factory() as session:
        assert len(session.scalars(select(orm.ScoutReportRow)).all()) == 1


def test_a_missing_slot_reaches_the_database_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """链路这一端也不许把读空的格子补成 0。

    `named_counts` 只吐出三个舰种，第四个就是「没读出来」。它必须原样落成 NULL——
    补成 0 就是把「没看清」记成「这里是空的」，下一轮据此判「不值得打」。
    """
    target = Coordinate(2, 137, 4)
    loop = _loop(
        tmp_path,
        monkeypatch,
        [
            _Screens(
                target=target,
                at="11/08/2026 03:32:11",
                counts={"深空吞噬者": 0, "噬能截击者": 1, "收割者": 0},
            )
        ],
    )

    found = loop.collect_scout_reports([target])

    assert found[target].verdict == "UNREADABLE"
    with loop.factory() as session:
        counts = {
            row.ship_type: row.count
            for row in session.scalars(select(orm.ScoutTriggerShipRow)).all()
        }
    assert counts == {"深空吞噬者": 0, "噬能截击者": 1, "收割者": 0, "钛能守卫者": None}


# -- 补录入口 ----------------------------------------------------------------


def test_the_backfill_reads_every_budgeted_mail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """补录不「收齐就走」：预算内的每一封都要看一遍。"""
    loop = _loop(
        tmp_path,
        monkeypatch,
        [
            _Screens(target=Coordinate(2, 137, position), at=f"11/08/2026 0{position}:00:00")
            for position in (1, 2, 3, 4)
        ],
    )

    read, written = loop.backfill_scout_reports(not_before=datetime(2026, 8, 11, tzinfo=UTC))

    assert (read, written) == (4, 4)
    assert len(_stored_targets(loop)) == 4


def test_the_backfill_does_not_rewrite_what_is_already_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """跑第二遍不该多出一行——补录和活链路可以随便交叉跑。"""
    mails = [_Screens(target=Coordinate(2, 137, 4), at="11/08/2026 03:32:11")]
    loop = _loop(tmp_path, monkeypatch, mails)

    loop.backfill_scout_reports()
    read, written = loop.backfill_scout_reports()

    assert (read, written) == (1, 0)
    assert len(_stored_targets(loop)) == 1


def test_the_backfill_never_reaches_for_the_real_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会动操作系统的调用不许待在这个方法里。

    写这个文件时它真的待在里面过：`backfill_scout_reports` 开头调
    `ensure_game_window()`，于是一条本该纯内存的单元测试伸手去改真实窗口的尺寸，
    连试三次改那个 1539×874 的窗口才报错退出。校几何和查会话现在在
    `prepare_for_mailbox()` 里，只由实机入口调。
    """
    from evo_helper.game import game_window

    reached: list[int] = []
    monkeypatch.setattr(game_window, "ensure_game_window", lambda *a, **k: reached.append(1))
    loop = _loop(tmp_path, monkeypatch, [])

    loop.backfill_scout_reports()

    assert reached == []


def test_the_backfill_asks_for_scout_reports_with_a_bigger_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """筛的仍是「侦察报告」这一类，预算比活链路那两个上限大。

    预算不放大就白跑：活链路的 8 封是按「一轮在等 6–8 份」定的，而补录要翻的是
    一整天——2026-08-11 光重复侦察就 25 发。
    """
    loop = _loop(tmp_path, monkeypatch, [])

    loop.backfill_scout_reports()

    (scan,) = loop.scans
    assert scan["wanted"] is ReportKind.SCOUT
    assert scan["max_opens"] > pirate_loop.MAIL_MAX_OPENS
    assert scan["max_pages"] > pirate_loop.MAIL_SCAN_PAGES
