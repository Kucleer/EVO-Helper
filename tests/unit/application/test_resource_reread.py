"""存档面板重跑之后「要改哪几格」的算账。

守的是三件事，每一件都在改历史数据这条路径上没有第二次机会：

- **全有或全无一个字都没放松**：12 格没读全就整份跳过，一格都不写，更不补 0。
- **算出来的是逐格的差**：打印出来的是哪几格、落库的就是哪几格。
- **幂等**：差落库之后再算一遍必须是空的，不然重跑一次就多一份改动。

用到的数字串是随手编的，不取自任何一份真实战报。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from evo_helper.application.resource_reread import (
    PlanKind,
    RereadSummary,
    plan_report,
    skipped_plan,
    slot_changes,
)
from evo_helper.domain.battle_resources import GAINED_SLOT_COUNT
from evo_helper.domain.records import BattleResourceEntry

#: 12 格原文，读全了的样子。第 3/7/10/11 格是 0，也就是「库里不该有这几行」。
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

#: `FULL_GRID` 解出来的非零条目。
WANTED = (
    BattleResourceEntry(slot=0, amount=486_200, approximate=True, uncertainty=50),
    BattleResourceEntry(slot=1, amount=12_100, approximate=True, uncertainty=50),
    BattleResourceEntry(slot=2, amount=272_000, approximate=True, uncertainty=500),
    BattleResourceEntry(slot=4, amount=17),
    BattleResourceEntry(slot=5, amount=4),
    BattleResourceEntry(slot=6, amount=233),
    BattleResourceEntry(slot=8, amount=66),
    BattleResourceEntry(slot=9, amount=8),
)


class TestTheAllOrNothingGateHolds:
    def test_a_single_unread_cell_voids_the_whole_report(self) -> None:
        """⚠️ 一格读不出，整份跳过——**这是最想被放松的一条**。

        放松了的话，读到的 11 格进了库，剩下那一格会被后来的人当成 0
        （这张表里「没有这一行 = 这一格是 0」），一次读不全就此变成一个凭空
        捏造的零，而且库里看不出来。要提高的是读得出，不是降低要求。
        """
        cells = list(FULL_GRID)
        cells[6] = ""

        plan = plan_report(uuid4(), cells, ())

        assert plan.kind is PlanKind.SKIPPED
        assert plan.changes == ()
        assert plan.writes == {}

    def test_the_skip_reason_names_which_cells_came_back_empty(self) -> None:
        """跳过时要说清「当时看到了什么」，否则下一次还是只能靠猜。"""
        cells = list(FULL_GRID)
        cells[0] = ""
        cells[6] = ""

        plan = plan_report(uuid4(), cells, ())

        assert plan.skip_reason is not None
        assert "[0, 6]" in plan.skip_reason

    def test_an_unreadable_panel_is_a_skip_too_but_says_so(self) -> None:
        """图本身读不了（尺寸不符、解不开）也算跳过，但原因得说清是图的问题。"""
        plan = skipped_plan(uuid4(), "存档面板是 520x694，版面标定的是 520x695")

        assert plan.kind is PlanKind.SKIPPED
        assert plan.cells == ("",) * GAINED_SLOT_COUNT
        assert plan.skip_reason is not None
        assert "520x694" in plan.skip_reason

    def test_a_report_that_reads_through_is_not_skipped(self) -> None:
        """读全了就该有结论——不然「跳过」这条判据是常真的，什么也没守住。"""
        plan = plan_report(uuid4(), FULL_GRID, ())

        assert plan.kind is PlanKind.ADDED

    def test_a_grid_that_is_not_twelve_cells_is_a_programming_error(self) -> None:
        """格数不对是版面变了或者调用方接错了，不是「没读出来」，当场抛。"""
        with pytest.raises(ValueError, match="12 格"):
            plan_report(uuid4(), FULL_GRID[:11], ())


class TestTheDiffIsPerSlot:
    def test_a_report_with_no_rows_gets_every_non_zero_cell(self) -> None:
        """当年整块作废的那种：库里一行都没有，这次把非零的格子补上。"""
        plan = plan_report(uuid4(), FULL_GRID, ())

        assert plan.kind is PlanKind.ADDED
        assert [change.slot for change in plan.changes] == [0, 1, 2, 4, 5, 6, 8, 9]
        assert all(change.before is None for change in plan.changes)
        assert plan.writes == {entry.slot: entry for entry in WANTED}

    def test_zero_cells_never_become_rows(self) -> None:
        """⚠️ 读到 0 的格子**不写行**。这张表里 0 就是「没有这一行」。"""
        plan = plan_report(uuid4(), FULL_GRID, ())

        assert {3, 7, 10, 11} & plan.writes.keys() == set()

    def test_a_wrong_stored_number_shows_up_as_before_and_after(self) -> None:
        """库里那个错数要原样打出来：`旧值 -> 新值`，两头都在。"""
        stored = (BattleResourceEntry(slot=0, amount=466_200, approximate=True, uncertainty=50),)

        plan = plan_report(uuid4(), FULL_GRID, stored)

        assert plan.kind is PlanKind.UPDATED
        first = next(change for change in plan.changes if change.slot == 0)
        assert first.before is not None
        assert (first.before.amount, first.after and first.after.amount) == (466_200, 486_200)
        assert "466200 -> 486200" in first.describe()

    def test_a_row_that_should_be_zero_gets_deleted(self) -> None:
        """重跑读到 0、库里却有一行，说明那一行当年读错了：删掉它。

        留着的话页面上会显示一笔从来没捞到过的收获，而那正是这次要修的事。
        """
        stored = (*WANTED, BattleResourceEntry(slot=3, amount=999))

        plan = plan_report(uuid4(), FULL_GRID, stored)

        assert plan.kind is PlanKind.UPDATED
        assert [(change.slot, change.after) for change in plan.changes] == [(3, None)]

    def test_precision_marks_count_as_a_difference(self) -> None:
        """数量一样、精度标记不一样也算改动。

        `approximate` 与 `uncertainty` 记的是「这个数准到什么程度」，页面照着
        它们写误差范围；只比数量的话，一个近似值会被显示得像精确读到的。
        """
        stored = (BattleResourceEntry(slot=4, amount=17, approximate=True, uncertainty=500),)

        changes = slot_changes(stored, (BattleResourceEntry(slot=4, amount=17),))

        assert [change.slot for change in changes] == [4]


class TestRunningItTwiceChangesNothing:
    def test_replanning_against_the_written_state_is_empty(self) -> None:
        """⚠️ 幂等：第一趟的结果落库之后再算一遍，一格都不该动。

        不成立的话，这个工具每跑一次就往库里写一批「改动」，而每一次都会写进
        `system_log`——事后翻日志的人分不清哪一次是真的改了数据。
        """
        first = plan_report(uuid4(), FULL_GRID, ())

        second = plan_report(first.report_id, FULL_GRID, tuple(WANTED))

        assert second.kind is PlanKind.UNCHANGED
        assert second.changes == ()
        assert second.writes == {}

    def test_an_all_zero_report_with_no_rows_is_already_settled(self) -> None:
        """12 格全 0 且库里一行都没有：读全了，但没有任何改动。"""
        plan = plan_report(uuid4(), ("0",) * GAINED_SLOT_COUNT, ())

        assert (plan.kind, plan.changes) == (PlanKind.UNCHANGED, ())


class TestTheTally:
    def test_the_summary_counts_each_kind_and_the_slots(self) -> None:
        """那份要交给人看的账：几份读全、几份跳过、新增/修改各几份、共几格。"""
        plans = [
            plan_report(uuid4(), FULL_GRID, ()),
            plan_report(uuid4(), FULL_GRID, tuple(WANTED)),
            plan_report(
                uuid4(),
                FULL_GRID,
                (BattleResourceEntry(slot=0, amount=466_200, approximate=True, uncertainty=50),),
            ),
            plan_report(uuid4(), ("", *FULL_GRID[1:]), ()),
        ]

        summary = RereadSummary.of(plans)

        assert (summary.total, summary.skipped, summary.unchanged) == (4, 1, 1)
        assert (summary.added, summary.updated) == (1, 1)
        # 新增那份 8 格，修改那份 slot 0 一格改数、其余 7 格是新增。
        assert summary.changed_slots == 8 + 8
