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

from evo_helper.domain.scheduler import EXIT_RANKING_INCOMPLETE
from evo_helper.game.ranking_ui import (
    BLIND_SCROLLS_MAX,
    BOT_DETECTION_BUDGET_SCROLLS,
    NAME_SAMPLE_EVERY_SCROLLS,
    READ_ATTEMPTS,
    ROWS_PER_SCROLL,
)
from evo_helper.tools.ranking_scan import (
    BlindWalk,
    ScanProgress,
    ScanStage,
    completion_message,
    exit_code_for_stretch,
    read_name_column_confirming,
    sample_overlap,
    scroll_through_humans,
)

#: 一屏真人的名字列长什么样（`--psm 6` 多行读出来的原样形状）。
HUMANS = "unkn0wn\nXXxxNAZIMxxXX\nhalo\nCocyte\n探险12"
BOTS = "bot_4_155_13\nbot_4_30_12\nbot_4_183_20"


class _Board:
    """一块可以往下滚的榜单。`screens` 是每一屏名字列会读到的字符串。

    ⚠️ `scroll`（检测段慢拖一屏）与 `spin`（盲滚段一次连拨）**分开记**：
    这次改动最容易漏的就是把两者合成一种动作，而合成之后 70 次等待原样还在。
    """

    def __init__(self, screens: list[str]) -> None:
        self.screens = screens
        self.at = 0
        self.scrolls = 0
        self.spun: list[int] = []
        self.reads = 0
        self.waits: list[float] = []

    def scroll(self) -> None:
        self.scrolls += 1
        self.at = min(len(self.screens) - 1, self.at + 1)

    def spin(self, rows: int) -> BlindWalk:
        """盲滚一趟。假的那条路原样走完请求的行数，屏号不动（这块板子按屏索引）。

        `measured=True`：滚轮那条路是闭环的（拨完读一次、不够就补拨），
        常态下「走了多少行」是量出来的。测不出的那一支单独有用例。
        """
        self.spun.append(rows)
        return BlindWalk(rows=rows, measured=True)

    def read(self) -> str:
        self.reads += 1
        return self.screens[self.at]

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)


def _run(
    board: _Board,
    *,
    blind_rows: int = 0,
    detection_budget: int = 5,
    said: list[str] | None = None,
    recorded: list[tuple[str, dict[str, object]]] | None = None,
    progress: ScanProgress | None = None,
):  # type: ignore[no-untyped-def]
    return scroll_through_humans(
        scroll=board.scroll,
        spin=board.spin,
        read_names=board.read,
        wait=board.wait,
        blind_rows=blind_rows,
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
            spin=board.spin,
            read_names=lambda: next(flaky),
            wait=board.wait,
            blind_rows=0,
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


# -- （C1）盲滚段：一次连拨，不是循环慢拖 --------------------------------------


class TestTheBlindPhaseSpinsOnce:
    """⚠️ **这一节是整次改动的要害。**

    原先盲滚段是 `for _ in range(blind_scrolls): scroll()`，每屏末尾等 2 秒——
    生产实测整段 294.6 秒 / 70 屏。收益**全部**来自把那些等待合并成一次，所以
    「只调一次 `spin`」这件事必须被钉住：改回循环调用照样能采到数，只是又慢回去，
    而慢回去在日志里看不出是回归。
    """

    def test_the_blind_phase_spins_once_instead_of_scrolling_per_screen(self) -> None:
        board = _Board([HUMANS, HUMANS, HUMANS, BOTS])

        stretch = _run(board, blind_rows=500, detection_budget=10)

        assert board.spun == [500], "一次，不是 70 次"
        assert board.scrolls == 3, "只有检测段在慢拖"
        assert stretch.reached_bots is True

    def test_the_detection_phase_still_slow_drags_every_screen(self) -> None:
        """⚠️ **检测段一个字都不许换成滚轮。**

        滚轮会把列表停在非整行位置，而 `rows_from_image` 是按
        `ROW_FIRST_Y + k×ROW_PITCH` 逐行裁剪的——偏了就横跨两行，名字全糊。
        实测过一次：画面清晰，`rows_from_image` 只读出 2 个名次。
        所以检测段翻几屏就得有几次 `scroll()`，一次 `spin()` 都不许有。
        """
        board = _Board([HUMANS])

        _run(board, blind_rows=300, detection_budget=7)

        assert board.scrolls == 7
        assert board.spun == [300], "盲滚段拨过一次之后，检测段不许再拨"

    def test_zero_rows_never_calls_spin_at_all(self) -> None:
        """0 行是最保守的合法取值：连 `spin` 都不该调。"""
        board = _Board([HUMANS])

        _run(board, blind_rows=0, detection_budget=3)

        assert board.spun == []

    def test_the_rows_account_adds_the_blind_rows_to_the_detected_screens(self) -> None:
        """对外的账是**行**：盲滚的行 + 检测段的屏 × 每屏行数。

        ⚠️ 这个数就是喂给自标定的实测量（「翻了 N 行到达 bot 区」）。把两段的单位
        混起来算，标定会静悄悄给出一个错了 8.3 倍的盲滚行数。
        """
        board = _Board([HUMANS, HUMANS, HUMANS, BOTS])

        stretch = _run(board, blind_rows=500, detection_budget=10)

        assert stretch.detection_scrolls == 3
        assert stretch.rows == 500 + round(3 * ROWS_PER_SCROLL)

    def test_the_spun_rows_and_not_the_requested_rows_are_what_count(self) -> None:
        """账记的是 `spin` **返回**的行数，不是请求的行数。

        ⚠️ 行 → 格那一步要取整，取整之后就已经不是原来那个行数了。把请求值当成
        走过的距离，误差会一路带到自标定的输入上。
        """
        board = _Board([BOTS])
        # 请求 500，实测只走了 480
        board.spin = lambda rows: BlindWalk(  # type: ignore[assignment,method-assign]
            rows=480, measured=True
        )

        stretch = _run(board, blind_rows=500, detection_budget=5)

        assert stretch.rows == 480


# -- （C2）检测预算的形状 -------------------------------------------------------


class TestTheDetectionBudgetIsIndependentOfTheBlindPhase:
    """⚠️ **原先的耦合是隐式且反向的。**

    `human_scrolls = 140` 是 `scan()` 上一个裸的默认形参，判据写作
    `scrolled >= human_scrolls`，而 `scrolled` 从 `blind_scrolls` 起步——
    于是**盲拖调大，检测预算等量缩小**，没有注释、没有测试。而盲拖屏数是会
    自动标定的，也就是说那个预算会在没人察觉的情况下一天天变小。

    换成行口径之后连相减的机会都没有了（两段单位不同），但判据还得钉住：
    `scrolled` 从 0 起步，预算就是检测段自己的屏数。
    """

    def test_the_budget_survives_a_large_blind_spin(self) -> None:
        board = _Board([HUMANS])

        stretch = _run(board, blind_rows=600, detection_budget=5)

        assert stretch.detection_scrolls == 5, "盲滚 600 行之后仍然要有 5 屏检测预算"
        assert board.scrolls == 5

    def test_a_small_blind_spin_gets_the_same_budget(self) -> None:
        board = _Board([HUMANS])

        stretch = _run(board, blind_rows=20, detection_budget=5)

        assert stretch.detection_scrolls == 5

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


# -- （E）退出码不许再占 argparse 的 2 ------------------------------------------


class TestTheExitCode:
    def test_reaching_the_bots_is_a_clean_zero(self) -> None:
        board = _Board([BOTS])

        assert exit_code_for_stretch(_run(board)) == 0

    def test_stopping_short_uses_the_ranking_specific_code(self) -> None:
        """⚠️ **不许是 2。** 2 是 `argparse` 的，两个含义共用一个值之后，
        真出参数错误时 `mission_runs.exit_code` 里那个 2 就分辨不出来了。
        """
        board = _Board([HUMANS])

        code = exit_code_for_stretch(_run(board))

        assert code == EXIT_RANKING_INCOMPLETE
        assert code != 2


# -- （F）收尾措辞能区分「被掐」与「跑满」 --------------------------------------


class TestTheClosingLineTellsThemApart:
    """⚠️ 原先两种情况说的是同一句「逐屏写入 0 条」，我据此对用户下过错误结论。"""

    def test_a_run_cut_short_in_the_blind_phase_says_which_phase(self) -> None:
        progress = ScanProgress(stage=ScanStage.BLIND, blind_rows=700, human_rows=0)

        line = completion_message(progress, written=0, suspect=0, outcome=0)

        assert "盲滚中" in line
        assert "盲滚 700 行" in line

    def test_a_run_that_used_the_whole_budget_says_so_instead(self) -> None:
        progress = ScanProgress(
            stage=ScanStage.DETECTING, blind_rows=700, human_rows=1198, detection_scrolls=60
        )

        line = completion_message(progress, written=0, suspect=0, outcome=69)

        assert "检测中" in line
        assert "1198 行" in line
        assert "盲滚 700 行 + 检测 60 屏" in line

    def test_the_two_units_are_never_collapsed_into_one_number(self) -> None:
        """⚠️ **盲滚是行、检测是屏，句子里就得写不一样。**

        两者差 8.3 倍。把它们凑成一个数会让「盲滚 700」看起来像 700 屏，
        而那正是这次改口径最容易错的地方——错了也不报错。
        """
        line = completion_message(
            ScanProgress(
                stage=ScanStage.DETECTING, blind_rows=700, human_rows=725, detection_scrolls=3
            ),
            written=0,
            suspect=0,
            outcome=69,
        )

        assert "盲滚 700 行" in line
        assert "检测 3 屏" in line
        assert "盲滚 700 屏" not in line

    def test_the_two_lines_are_not_the_same_sentence(self) -> None:
        """本类的要害：**两句话必须不一样**，否则日志分不开这两种。

        ⚠️ 两边的 `outcome`、`written`、`suspect` 全都一样，只有「走到了哪一段、
        走了多少行」不同——这正是原先那句话丢掉的全部信息。拿退出码去凑差别是
        不算数的：真正把我误导了的那次，两边在日志里长得一模一样。
        """
        cut_short = completion_message(
            ScanProgress(
                stage=ScanStage.DETECTING, blind_rows=700, human_rows=725, detection_scrolls=3
            ),
            written=0,
            suspect=0,
            outcome=69,
        )
        exhausted = completion_message(
            ScanProgress(
                stage=ScanStage.DETECTING, blind_rows=700, human_rows=1198, detection_scrolls=60
            ),
            written=0,
            suspect=0,
            outcome=69,
        )

        assert cut_short != exhausted

    def test_a_run_cut_short_is_never_reported_as_finished(self) -> None:
        """被打断的那一趟**不许**说「完成」——那正是让人放心不管的那两个字。"""
        line = completion_message(
            ScanProgress(stage=ScanStage.BLIND, blind_rows=700, human_rows=0),
            written=0,
            suspect=0,
            outcome=0,
        )

        assert "完成" not in line

    def test_a_normal_finish_still_reads_as_finished(self) -> None:
        progress = ScanProgress(
            stage=ScanStage.CLOSED,
            blind_rows=700,
            human_rows=758,
            detection_scrolls=7,
            collect_scrolls=140,
        )

        line = completion_message(progress, written=812, suspect=0, outcome=0)

        assert line.startswith("军事榜采集完成：")
        assert "采集段滚了 140 屏" in line
        assert "逐屏写入 812 条" in line

    def test_the_progress_is_updated_as_the_stretch_runs(self) -> None:
        """`ScanProgress` 是 `finally` 那一句唯一的信息来源，所以它得跟着走。"""
        board = _Board([HUMANS])
        progress = ScanProgress()

        _run(board, blind_rows=300, detection_budget=4, progress=progress)

        assert progress.blind_rows == 300
        assert progress.detection_scrolls == 4
        assert progress.human_rows == 300 + round(4 * ROWS_PER_SCROLL)
        assert progress.stage is ScanStage.DETECTING
