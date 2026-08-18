"""离线重跑存档面板、回填收获明细的整条路径，跑在真库上。

这条路径**改的是历史数据**，所以判据比新写一行严得多，逐条钉在下面：

- **干跑一个字都不写库**（连 `system_log` 都不写——它和数据住同一个库里）；
- **`--apply` 才写**；
- **12 格没读全的整份跳过**，一格都不写，更不补 0；
- **幂等**：同一份跑两次结果一致，不产生重复行；
- ⚠️ **只碰 `battle_report_resources`**：`battle_reports` 的 `outcome`、
  `attacker_units`、`defender_units`、`attacker_losses`、`defender_losses`、
  `match_status` 一个字段都不许变。

面板是合成的（`support.panels`），数字是随手编的，不取自任何一份真实战报。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.records import BattleResourceEntry
from evo_helper.storage import models as orm
from evo_helper.storage.report_resources import ReportResourceRepository
from evo_helper.storage.report_screenshots import ReportScreenshotRepository
from evo_helper.tools.reread_report_resources import main
from support.database import scratch_database_url

pytest.importorskip("PIL.Image", reason="要 Pillow 才画得出面板")

from support.panels import panel_bytes  # noqa: E402 - importorskip 必须在前

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)

#: 一份读得全的面板。第 3/7/10/11 格是 0，也就是库里不该有这几行。
FULL_GRID: tuple[str, ...] = (
    "486.2K",
    "12.1K",
    "272K",
    "0",
    "17",
    "4",
    "233",
    "0",
    "66",
    "8",
    "0",
    "0",
)

#: 读全之后该进库的非零格子。
WANTED_ROWS = [
    (0, 486_200, True, 50),
    (1, 12_100, True, 50),
    (2, 272_000, True, 500),
    (4, 17, False, 0),
    (5, 4, False, 0),
    (6, 233, False, 0),
    (8, 66, False, 0),
    (9, 8, False, 0),
]

#: `battle_reports` 上这一趟绝对不许动的字段。
UNTOUCHABLE = (
    "outcome",
    "attacker_units",
    "defender_units",
    "attacker_losses",
    "defender_losses",
    "match_status",
    "dispatch_id",
)


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    """和 `session_factory` 夹具指同一个 SQLite 文件——命令行要靠它连库。"""
    return scratch_database_url(tmp_path, "test.db")


def seed_report(
    session_factory: sessionmaker[Session], cells: tuple[str, ...], *, position: int = 6
) -> UUID:
    """一份带着完整战果的战报 + 它那张存档面板。"""
    report_id = uuid4()
    with session_factory() as session:
        session.add(
            orm.BattleReportRow(
                id=report_id,
                reported_at_utc=NOW,
                raw_time_text="08/18/2026 12:00",
                attacker_origin_galaxy=4,
                attacker_origin_system=200,
                attacker_origin_position=19,
                defender_target_galaxy=4,
                defender_target_system=20,
                defender_target_position=position,
                outcome="VICTORY",
                attacker_units=500,
                defender_units=3_550,
                attacker_losses=12,
                defender_losses=3_550,
                match_status="MATCHED",
            )
        )
        session.commit()
    image = panel_bytes(cells)
    ReportScreenshotRepository(session_factory).save(
        report_id, image_bytes=image, width=520, height=695, captured_at_utc=NOW
    )
    return report_id


def stored_rows(
    session_factory: sessionmaker[Session], report_id: UUID
) -> list[tuple[int, int, bool, int]]:
    with session_factory() as session:
        rows = session.scalars(
            select(orm.BattleReportResourceRow)
            .where(orm.BattleReportResourceRow.report_id == report_id)
            .order_by(orm.BattleReportResourceRow.slot)
        ).all()
    return [(row.slot, row.amount, row.approximate, row.uncertainty) for row in rows]


def battle_report_fields(
    session_factory: sessionmaker[Session], report_id: UUID
) -> dict[str, object]:
    with session_factory() as session:
        row = session.get(orm.BattleReportRow, report_id)
        assert row is not None
        return {name: getattr(row, name) for name in UNTOUCHABLE}


class TestTheDryRunIsReallyDry:
    def test_it_writes_nothing_at_all(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """⚠️ 默认干跑：打印要改什么，库里一行都不多、一行都不少。

        这条是整个工具的第一道安全阀。它坏掉的样子是「跑一下看看」直接改了
        生产数据——而这条路径改的是**历史**数据，改错了没有第二份可对。
        """
        report_id = seed_report(session_factory, FULL_GRID)

        assert main(["--database-url", database_url]) == 0

        assert stored_rows(session_factory, report_id) == []

    def test_it_does_not_even_write_the_system_log(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """⚠️ 连日志都不写：`system_log` 和这些数据住在同一个库里。

        装上日志出口的干跑就不再是干跑了——它会往生产库写行，而用户跑干跑正是
        为了「先看看，什么都别动」。
        """
        seed_report(session_factory, FULL_GRID)

        main(["--database-url", database_url])

        with session_factory() as session:
            assert session.scalars(select(orm.SystemLogRow)).all() == []

    def test_it_prints_the_old_and_the_new_value(
        self,
        session_factory: sessionmaker[Session],
        database_url: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """干跑的输出必须够人核对：哪份战报、哪一格、旧值 → 新值。"""
        report_id = seed_report(session_factory, FULL_GRID)
        ReportResourceRepository(session_factory).apply_slot_changes(
            report_id,
            {0: BattleResourceEntry(slot=0, amount=466_200, approximate=True, uncertainty=50)},
        )

        main(["--database-url", database_url])

        printed = capsys.readouterr().out
        assert "466200 -> 486200" in printed
        assert "4:20:6" in printed


class TestApplyingWritesTheHaul:
    def test_a_report_that_never_had_rows_gets_them(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """当年整块作废的那 29 份走的就是这条：库里从没有行，到有 8 行。"""
        report_id = seed_report(session_factory, FULL_GRID)

        assert main(["--database-url", database_url, "--apply"]) == 0

        assert stored_rows(session_factory, report_id) == WANTED_ROWS

    def test_a_wrong_stored_number_gets_corrected(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """库里已有的错数被改对，同一格不会因此多出第二行。"""
        report_id = seed_report(session_factory, FULL_GRID)
        ReportResourceRepository(session_factory).apply_slot_changes(
            report_id,
            {0: BattleResourceEntry(slot=0, amount=466_200, approximate=True, uncertainty=50)},
        )

        main(["--database-url", database_url, "--apply"])

        rows = stored_rows(session_factory, report_id)
        assert rows == WANTED_ROWS
        assert [row for row in rows if row[0] == 0] == [(0, 486_200, True, 50)]

    def test_a_row_that_should_be_zero_is_removed(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """重跑读到 0、库里却有一行：删掉。留着就是一笔从没捞到过的收获。"""
        report_id = seed_report(session_factory, FULL_GRID)
        ReportResourceRepository(session_factory).apply_slot_changes(
            report_id, {3: BattleResourceEntry(slot=3, amount=999)}
        )

        main(["--database-url", database_url, "--apply"])

        assert stored_rows(session_factory, report_id) == WANTED_ROWS

    def test_every_change_lands_in_the_system_log(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """⚠️ 改历史数据必须留痕：哪份战报、哪一格、旧值 → 新值。

        事后没有第二个地方能回答「这个数是什么时候变的」——原始观测已经被
        覆盖掉了。
        """
        report_id = seed_report(session_factory, FULL_GRID)
        ReportResourceRepository(session_factory).apply_slot_changes(
            report_id,
            {0: BattleResourceEntry(slot=0, amount=466_200, approximate=True, uncertainty=50)},
        )

        main(["--database-url", database_url, "--apply"])

        with session_factory() as session:
            rows = session.scalars(
                select(orm.SystemLogRow).where(orm.SystemLogRow.source == "resource-reread")
            ).all()
            payloads = [row.payload_json or "" for row in rows]
        assert any(
            str(report_id) in payload
            and '"before": 466200' in payload
            and '"after": 486200' in payload
            for payload in payloads
        )


class TestTheAllOrNothingGateSurvivesTheWholePath:
    def test_a_panel_with_one_unread_cell_writes_nothing(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """⚠️ 一格没读出来，整份跳过——**放松这条最省事，也最致命**。

        写进去的话，读到的 11 格进了库，剩下那一格会被当成 0（这张表里
        「没有这一行 = 这一格是 0」），凭空多一个从来没观测到的零。
        """
        blank = (*FULL_GRID[:6], "", *FULL_GRID[7:])
        report_id = seed_report(session_factory, blank)

        main(["--database-url", database_url, "--apply"])

        assert stored_rows(session_factory, report_id) == []

    def test_the_readable_report_still_lands(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """跳过是**逐份**的：一份读不全不该连累另一份。"""
        blank = (*FULL_GRID[:6], "", *FULL_GRID[7:])
        broken = seed_report(session_factory, blank, position=7)
        good = seed_report(session_factory, FULL_GRID, position=8)

        main(["--database-url", database_url, "--apply"])

        assert stored_rows(session_factory, broken) == []
        assert stored_rows(session_factory, good) == WANTED_ROWS

    def test_the_skip_is_recorded_with_what_it_saw(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """挡掉的那一刻要说清为什么、当时看到了什么，否则下次还是只能猜。"""
        blank = (*FULL_GRID[:6], "", *FULL_GRID[7:])
        seed_report(session_factory, blank)

        main(["--database-url", database_url, "--apply"])

        with session_factory() as session:
            rows = session.scalars(
                select(orm.SystemLogRow).where(orm.SystemLogRow.level == "WARNING")
            ).all()
            messages = [row.message for row in rows]
        assert any("没读全" in message for message in messages)


class TestRunningItTwice:
    def test_the_second_run_changes_nothing_and_duplicates_nothing(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """⚠️ 幂等。第二趟必须是「无变化」，一行都不许多出来。

        不成立的话，`report_id + slot` 上那条唯一约束迟早会撞上——而在那之前，
        每一次重跑都会往 `system_log` 里写一批假的「改动」。
        """
        report_id = seed_report(session_factory, FULL_GRID)
        main(["--database-url", database_url, "--apply"])
        first = stored_rows(session_factory, report_id)

        assert main(["--database-url", database_url, "--apply"]) == 0

        assert stored_rows(session_factory, report_id) == first == WANTED_ROWS

    def test_the_second_run_writes_no_change_log(
        self,
        session_factory: sessionmaker[Session],
        database_url: str,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """第二趟什么都没改，就不该报告「改了 N 份」。"""
        seed_report(session_factory, FULL_GRID)
        main(["--database-url", database_url, "--apply"])
        capsys.readouterr()

        main(["--database-url", database_url, "--apply"])

        assert "已写库：0 份" in capsys.readouterr().out


class TestNothingElseIsTouched:
    def test_the_battle_report_row_is_byte_for_byte_the_same(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """⚠️ `battle_reports` 一个字段都不许动。

        `outcome` / 双方单位 / 双方损失 / `match_status` / `dispatch_id` 是当年
        那一屏读出来的观测与认领结果。这一趟既没重读它们，也没资格拿今天的一次
        离线重跑去覆盖它们。
        """
        report_id = seed_report(session_factory, FULL_GRID)
        before = battle_report_fields(session_factory, report_id)

        main(["--database-url", database_url, "--apply"])

        assert battle_report_fields(session_factory, report_id) == before

    def test_the_screenshot_is_left_alone(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """图是唯一的原始观测，重跑只读它。删了就再也重跑不了了。"""
        report_id = seed_report(session_factory, FULL_GRID)

        main(["--database-url", database_url, "--apply"])

        shot = ReportScreenshotRepository(session_factory).load(report_id)
        assert shot is not None
        assert shot.image_bytes == panel_bytes(FULL_GRID)


class TestTheRowWriterRefusesNonsense:
    def test_writing_a_zero_row_is_refused(self, session_factory: sessionmaker[Session]) -> None:
        """⚠️ 数量 0 不写行。这张表里 0 就是「没有这一行」，写进去会让后来的人
        分不清「读到了 0」和「多写了一行」。要表达 0，传 `None`。
        """
        report_id = seed_report(session_factory, FULL_GRID)

        with pytest.raises(ValueError, match="请传 None"):
            ReportResourceRepository(session_factory).apply_slot_changes(
                report_id, {3: BattleResourceEntry(slot=3, amount=0)}
            )

    def test_a_slot_outside_the_grid_is_refused(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """网格只有 12 格。越界的槽位是调用方算错了，不是一格新资源。"""
        report_id = seed_report(session_factory, FULL_GRID)

        with pytest.raises(ValueError, match="不在 0..11"):
            ReportResourceRepository(session_factory).apply_slot_changes(report_id, {12: None})

    def test_an_entry_filed_under_another_slot_is_refused(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        """条目自带 slot，和键对不上就是错位——错位会把 A 资源的数量记到 B 上。"""
        report_id = seed_report(session_factory, FULL_GRID)

        with pytest.raises(ValueError, match="slot=5"):
            ReportResourceRepository(session_factory).apply_slot_changes(
                report_id, {4: BattleResourceEntry(slot=5, amount=17)}
            )


class TestPickingOneReport:
    def test_only_the_named_report_is_planned(
        self, session_factory: sessionmaker[Session], database_url: str
    ) -> None:
        """`--report` 只跑那一份。回填出岔子时要能一份一份来。"""
        first = seed_report(session_factory, FULL_GRID, position=6)
        second = seed_report(session_factory, FULL_GRID, position=7)

        main(["--database-url", database_url, "--apply", "--report", str(first)])

        assert stored_rows(session_factory, first) == WANTED_ROWS
        assert stored_rows(session_factory, second) == []
