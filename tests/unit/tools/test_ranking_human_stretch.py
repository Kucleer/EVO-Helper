"""翻真人段：留现场、确认式判空、检测预算的形状，以及收尾那句话。

2026-08-18 排障留下的账（六轮「采不到」，其中只有一轮是真失败）：

- 那一轮里「列表根本没动」「开错了面板」「bot 名字读不出来」**完全不可分辨**，
  因为循环一个字现场都没留；
- 「名字列读到空」是**单帧**判定，读到空就当场判离页、整趟作废——而全历史三次
  触发在第 101 / 79 / 78 屏，bot 区就在 78–82，**两次是差一屏就到**；
- 收尾那句「逐屏写入 0 条」对「跑 2.2 分钟被用户掐」和「跑满预算一无所获」
  说的是同一句话，排障的人（我）据此对用户下过错误结论。

⚠️ 全程不碰游戏：滚动和读屏都是喂进来的清单。
"""

from __future__ import annotations

from evo_helper.game.ranking_ui import (
    BLIND_SCROLLS_MAX,
    BOT_DETECTION_BUDGET_SCROLLS,
    NAME_SAMPLE_EVERY_SCROLLS,
    READ_ATTEMPTS,
)
from evo_helper.tools.ranking_scan import (
    ScanProgress,
    ScanStage,
    completion_message,
    read_name_column_confirming,
    sample_overlap,
    scroll_through_humans,
)

#: 一屏真人的名字列长什么样（`--psm 6` 多行读出来的原样形状）。
HUMANS = "unkn0wn\nXXxxNAZIMxxXX\nhalo\nCocyte\n探险12"
BOTS = "bot_4_155_13\nbot_4_30_12\nbot_4_183_20"


class _Board:
    """一块可以往下滚的榜单。`screens` 是每一屏名字列会读到的字符串。"""

    def __init__(self, screens: list[str]) -> None:
        self.screens = screens
        self.at = 0
        self.scrolls = 0
        self.reads = 0
        self.waits: list[float] = []

    def scroll(self) -> None:
        self.scrolls += 1
        self.at = min(len(self.screens) - 1, self.at + 1)

    def read(self) -> str:
        self.reads += 1
        return self.screens[self.at]

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)


def _run(
    board: _Board,
    *,
    blind_scrolls: int = 0,
    detection_budget: int = 5,
    said: list[str] | None = None,
    recorded: list[tuple[str, dict[str, object]]] | None = None,
    progress: ScanProgress | None = None,
):  # type: ignore[no-untyped-def]
    return scroll_through_humans(
        scroll=board.scroll,
        read_names=board.read,
        wait=board.wait,
        blind_scrolls=blind_scrolls,
        detection_budget=detection_budget,
        say_line=(said.append if said is not None else (lambda _m: None)),
        record=(
            (lambda message, payload: recorded.append((message, payload)))
            if recorded is not None
            else (lambda _m, _p: None)
        ),
        progress=progress,
    )


# -- （B）名字列全空要确认式重读 ------------------------------------------------


class TestABlankNameColumnIsNotEvidence:
    """⚠️ **`game.ranking_nav` 模块头第一条规矩：「空结果不是证据」。**

    同一调用栈里所有别的读法都重读 3 次才认空（`_rows_confirming` /
    `_labels_confirming` / `preset_picker.read_names_confirming` /
    `vision.scan_reading.read_panel_confirming`）；只有这一处是单帧。
    """

    def test_one_blank_frame_is_read_again_instead_of_leaving_the_page(self) -> None:
        blanks = iter(["", HUMANS])

        text, attempts = read_name_column_confirming(lambda: next(blanks), lambda _s: None)

        assert text == HUMANS
        assert attempts == ("", HUMANS), "两次各读到什么要原样带出去"

    def test_only_three_blanks_in_a_row_count_as_off_page(self) -> None:
        reads = 0

        def read() -> str:
            nonlocal reads
            reads += 1
            return ""

        text, attempts = read_name_column_confirming(read, lambda _s: None)

        assert text == ""
        assert reads == READ_ATTEMPTS
        assert attempts == ("",) * READ_ATTEMPTS

    def test_a_single_blank_frame_does_not_throw_the_whole_run_away(self) -> None:
        """整趟测：第 3 屏读到一帧空，重读就好了——**不许**据此判离页。

        全历史三次触发在第 101 / 79 / 78 屏，而 bot 区就在 78–82。
        """
        board = _Board([HUMANS, HUMANS, "", HUMANS, BOTS])
        flaky = iter([HUMANS, HUMANS, "", HUMANS, HUMANS, BOTS])

        stretch = scroll_through_humans(
            scroll=board.scroll,
            read_names=lambda: next(flaky),
            wait=board.wait,
            blind_scrolls=0,
            detection_budget=10,
            say_line=lambda _m: None,
        )

        assert stretch.reached_bots is True

    def test_three_blanks_in_a_row_still_report_off_page(self) -> None:
        """真离页仍旧要认出来，而且**三次各读到什么**要进 payload。"""
        board = _Board([HUMANS, ""])
        recorded: list[tuple[str, dict[str, object]]] = []

        stretch = _run(board, detection_budget=10, recorded=recorded)

        assert stretch.reached_bots is False
        assert "离页" in stretch.reason
        assert recorded[-1][1]["reads"] == [""] * READ_ATTEMPTS


# -- （A）翻真人段要留下现场 ----------------------------------------------------


class TestLeavingEvidenceWhileScrollingThroughHumans:
    """⚠️ 判据是「出事时能不能只靠库里的日志定位」，2026-08-18 的答案是不能。"""

    def test_a_sample_is_taken_every_ten_screens(self) -> None:
        board = _Board([f"玩家{index}\n资源{index}\n探险{index}" for index in range(40)])

        stretch = _run(board, detection_budget=25)

        assert [sample.scrolled for sample in stretch.samples] == [
            NAME_SAMPLE_EVERY_SCROLLS,
            NAME_SAMPLE_EVERY_SCROLLS * 2,
        ]

    def test_each_sample_carries_the_name_column_and_the_overlap(self) -> None:
        board = _Board([f"玩家{index}\n资源{index}" for index in range(40)])

        stretch = _run(board, detection_budget=25)

        first, second = stretch.samples
        assert first.excerpt, "摘要不许是空的"
        assert first.overlap is None, "第一次抽样没有上一次可比"
        assert second.overlap is not None

    def test_a_list_that_never_moves_is_called_out_by_name(self) -> None:
        """⚠️ **这就是「列表根本没动」那一种。**

        重合率长期高 = 每一屏读到的都是同一批人，也就是拖动压根没生效
        （或者根本不在榜单页上）。到顶时必须说出来，而不是只报一句「翻满 N 屏」。
        """
        board = _Board([HUMANS])  # 怎么滚都是同一屏
        said: list[str] = []

        stretch = _run(board, detection_budget=25, said=said)

        assert all(sample.stuck for sample in stretch.samples if sample.overlap is not None)
        assert "一直没变" in "\n".join(said)

    def test_a_list_that_keeps_moving_says_so_instead(self) -> None:
        """反面：滚是滚了、只是没见到 bot。两种的善后完全不同。"""
        board = _Board([f"玩家{index}\n资源{index}\n探险{index}" for index in range(40)])
        said: list[str] = []

        _run(board, detection_budget=25, said=said)

        spoken = "\n".join(said)
        assert "一直在变" in spoken
        assert "一直没变" not in spoken

    def test_the_budget_verdict_carries_the_samples_into_the_payload(self) -> None:
        """到顶那一条要能只靠库里的记录复盘，所以整串抽样都进 `payload_json`。"""
        board = _Board([HUMANS])
        recorded: list[tuple[str, dict[str, object]]] = []

        _run(board, detection_budget=25, recorded=recorded)

        message, payload = recorded[-1]
        assert "仍没见到 bot" in message
        assert payload["last_name_excerpt"]
        assert payload["stuck_samples"] == len(payload["samples"]) - 1  # type: ignore[arg-type]

    def test_the_overlap_is_zero_for_two_unrelated_screens(self) -> None:
        assert sample_overlap("unkn0wn halo Cocyte", "bot_4_155_13 bot_4_30_12") < 0.4
        assert sample_overlap("unkn0wn halo", "unkn0wn halo") == 1.0


# -- （C）检测预算的形状 --------------------------------------------------------


class TestTheDetectionBudgetIsAddedNotSubtracted:
    """⚠️ **原先的耦合是隐式且反向的。**

    `human_scrolls = 140` 是 `scan()` 上一个裸的默认形参，判据写作
    `scrolled >= human_scrolls`，而 `scrolled` 从 `blind_scrolls` 起步——
    于是**盲拖调大，检测预算等量缩小**，没有注释、没有测试。而盲拖屏数是会
    自动标定的，也就是说那个预算会在没人察觉的情况下一天天变小。
    """

    def test_the_budget_survives_a_large_blind_scroll(self) -> None:
        board = _Board([HUMANS])

        stretch = _run(board, blind_scrolls=30, detection_budget=5)

        assert stretch.scrolled == 35, "盲拖 30 之后仍然要有 5 屏检测预算"
        assert board.scrolls == 35

    def test_a_small_blind_scroll_gets_the_same_budget(self) -> None:
        board = _Board([HUMANS])

        stretch = _run(board, blind_scrolls=2, detection_budget=5)

        assert stretch.scrolled == 7

    def test_the_budget_is_wide_enough_for_the_worst_measured_human_stretch(self) -> None:
        """⚠️ **这一条把预算和 `BLIND_SCROLLS_MAX` 绑在一起。**

        实测真人段跨度 72–82 屏（13 个样本）。最坏情况是用户把盲拖手填到上界
        `BLIND_SCROLLS_MAX`(62)，那时还需要 82 − 62 = 20 屏检测。预算必须**远大于**
        它——取 3 倍余量，因为真人段会随玩家增长往上漂，而余量不够的表现是
        「整趟白跑、一条都不写」，不是一条报错。
        """
        longest_human_stretch = 82
        worst_case = longest_human_stretch - BLIND_SCROLLS_MAX

        assert worst_case == 20
        assert BOT_DETECTION_BUDGET_SCROLLS >= worst_case * 3

    def test_the_budget_is_not_so_wide_that_a_broken_run_never_stops(self) -> None:
        """反向的界：预算也不该大到让一趟坏掉的采集空转半小时以上。

        一屏约 4.2 秒（实测），60 屏 ≈ 4.2 分钟。
        """
        assert BOT_DETECTION_BUDGET_SCROLLS <= 150


# -- （F）收尾措辞能区分「被掐」与「跑满」 --------------------------------------


class TestTheClosingLineTellsThemApart:
    """⚠️ 原先两种情况说的是同一句「逐屏写入 0 条」，我据此对用户下过错误结论。"""

    def test_a_run_cut_short_in_the_blind_phase_says_which_phase(self) -> None:
        progress = ScanProgress(stage=ScanStage.BLIND, blind_scrolls=70, human_scrolled=31)

        line = completion_message(progress, written=0, suspect=0, outcome=0)

        assert "盲拖中" in line
        assert "31 屏" in line

    def test_a_run_that_used_the_whole_budget_says_so_instead(self) -> None:
        progress = ScanProgress(stage=ScanStage.DETECTING, blind_scrolls=70, human_scrolled=130)

        line = completion_message(progress, written=0, suspect=0, outcome=69)

        assert "检测中" in line
        assert "130 屏" in line
        assert "盲拖 70 + 检测 60" in line

    def test_the_two_lines_are_not_the_same_sentence(self) -> None:
        """本类的要害：**两句话必须不一样**，否则日志分不开这两种。"""
        cut = completion_message(
            ScanProgress(stage=ScanStage.BLIND, blind_scrolls=70, human_scrolled=31),
            written=0,
            suspect=0,
            outcome=0,
        )
        exhausted = completion_message(
            ScanProgress(stage=ScanStage.DETECTING, blind_scrolls=70, human_scrolled=130),
            written=0,
            suspect=0,
            outcome=69,
        )

        assert cut != exhausted

    def test_a_normal_finish_still_reads_as_finished(self) -> None:
        progress = ScanProgress(
            stage=ScanStage.CLOSED, blind_scrolls=70, human_scrolled=79, collect_scrolls=140
        )

        line = completion_message(progress, written=812, suspect=0, outcome=0)

        assert line.startswith("军事榜采集完成：")
        assert "采集段滚了 140 屏" in line
        assert "逐屏写入 812 条" in line

    def test_the_progress_is_updated_as_the_stretch_runs(self) -> None:
        """`ScanProgress` 是 `finally` 那一句唯一的信息来源，所以它得跟着走。"""
        board = _Board([HUMANS])
        progress = ScanProgress()

        _run(board, blind_scrolls=3, detection_budget=4, progress=progress)

        assert progress.blind_scrolls == 3
        assert progress.human_scrolled == 7
        assert progress.stage is ScanStage.DETECTING
