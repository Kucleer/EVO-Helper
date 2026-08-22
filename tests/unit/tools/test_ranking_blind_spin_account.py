"""盲滚那一趟的账：记了什么、折合回多少行，以及慢拖那条回滚路。

⚠️ **这份账是这次改动唯一能事后自证的东西。** `ROWS_PER_NOTCH`(1.08) 只有 2 个
样本、1 台机器、1 次会话；它一漂，「盲滚 700 行」实际走的就不是 700 行，而这个
偏差是**静默的**——不报错、不少一条日志，只是采回来的数少一截。所以每一趟都得把
「发了几格、实测走了几行」记进库，事后才答得出「这个标定还成不成立」。

⚠️ 全程不碰游戏：滚轮那一层（`game.ranking_nav.spin_blind`）在这里是个假的。
"""

from __future__ import annotations

from evo_helper.game.ranking_nav import SpinResult
from evo_helper.game.ranking_ui import GLIDE_SETTLE_S, ROWS_PER_NOTCH, ROWS_PER_SCROLL
from evo_helper.tools.ranking_scan import (
    BlindSpinAccount,
    blind_spin_payload,
    drag_blind_rows,
    report_blind_spin,
    spin_blind_rows,
)


def _spin(rows: int, *, notches: int | None = None, seconds: float = 1.234) -> SpinResult:
    """一趟假盲滚。`notches` 不给就按标定算，和真的那一层一致。"""
    return SpinResult(
        rows_requested=rows,
        notches=round(rows / ROWS_PER_NOTCH) if notches is None else notches,
        spin_seconds=seconds,
    )


# -- 折合回来的行数 ------------------------------------------------------------


def test_the_rows_walked_come_from_the_notches_actually_sent() -> None:
    """⚠️ **返回值是「实发格数 × 标定」，不是传进来的行数。**

    行 → 格那一步要取整，取整之后就已经不是原来那个行数了。把请求值当成走过的
    距离记账，误差会一路带到「实测多少行到达 bot 区」上，而那正是自标定的输入。
    """
    account = BlindSpinAccount()

    walked = spin_blind_rows(500, spin=_spin, account=account)

    assert account.notches_sent == round(500 / ROWS_PER_NOTCH)
    assert walked == round(account.notches_sent * ROWS_PER_NOTCH)


def test_a_short_spin_that_only_managed_half_the_notches_reports_half_the_rows() -> None:
    """真发出去的格数少了一半，账上走过的行数就得跟着少一半。"""
    account = BlindSpinAccount()

    walked = spin_blind_rows(500, spin=lambda rows: _spin(rows, notches=231), account=account)

    assert account.rows_requested == 500
    assert account.notches_sent == 231
    assert walked == round(231 * ROWS_PER_NOTCH)


# -- 记了哪些账 ----------------------------------------------------------------


def test_the_account_carries_both_the_notches_and_the_requested_rows() -> None:
    """⚠️ **两个都要留。** 格数是真发生的事，行数是它乘标定算出来的；
    只留折合值就把两者的差别抹平了，而这条日志存在的意义就是让它们能对上。
    """
    account = BlindSpinAccount()

    spin_blind_rows(700, spin=lambda rows: _spin(rows, seconds=10.5), account=account)

    assert account.rows_requested == 700
    assert account.notches_sent == 648
    assert account.spin_seconds == 10.5
    assert account.glide_seconds == GLIDE_SETTLE_S


def test_zero_notches_means_no_glide_wait_was_made() -> None:
    """一格都没拨就一次都不等——账上也不许写成等过。"""
    account = BlindSpinAccount()

    walked = spin_blind_rows(0, spin=lambda rows: _spin(rows, notches=0), account=account)

    assert walked == 0
    assert account.glide_seconds == 0.0
    assert account.rows_measured is None


def test_the_measurement_is_skipped_when_nothing_was_spun() -> None:
    """一格都没拨，就没有「拨完之后走到第几名」这回事，别去读屏。"""
    reads = 0

    def measure() -> int | None:
        nonlocal reads
        reads += 1
        return 123

    spin_blind_rows(0, spin=lambda rows: _spin(rows, notches=0), measure_rows=measure)

    assert reads == 0


def test_a_measurement_that_reads_nothing_is_recorded_as_unknown() -> None:
    """⚠️ **测不出不是失败。**

    滚轮会把列表停在非整行位置，逐行裁剪读出来的名次会横跨两行（实测过一屏只读出
    2 个名次）。它只服务于日志、不参与任何判据，所以读不出就老实记 None，
    绝不许据此把这一趟判成失败。
    """
    account = BlindSpinAccount()

    walked = spin_blind_rows(500, spin=_spin, measure_rows=lambda: None, account=account)

    assert walked > 0, "读不出名次不影响这一趟走过的距离"
    assert account.rows_measured is None
    assert account.rows_per_notch_observed is None


# -- 要害：实测每格走了多少行 --------------------------------------------------


def test_the_observed_rows_per_notch_is_measured_over_the_notches_sent() -> None:
    """实测每格行数 = 实测走到第几名 ÷ 实发格数。**分母是格数，不是行数。**

    拿行数当分母就成了「实测 ÷ 请求」，那量的是「请求准不准」而不是「标定准不准」，
    而后者才是这条日志要回答的问题。
    """
    account = BlindSpinAccount()

    spin_blind_rows(
        500, spin=lambda rows: _spin(rows, notches=463), measure_rows=lambda: 500, account=account
    )

    assert account.rows_per_notch_observed == round(500 / 463, 3)


def test_an_observed_calibration_that_drifted_shows_up_as_a_different_number() -> None:
    """标定漂了就得看得出来——这条日志存在的全部理由。

    实发 463 格却只走了 400 行 = 每格 0.864 行，比标定的 1.08 少两成；
    「盲滚 700 行」实际只走 560 行，而这件事在别处一个字都看不出来。
    """
    account = BlindSpinAccount()

    spin_blind_rows(
        500, spin=lambda rows: _spin(rows, notches=463), measure_rows=lambda: 400, account=account
    )

    assert account.rows_per_notch_observed is not None
    assert account.rows_per_notch_observed < ROWS_PER_NOTCH * 0.9


# -- payload 的形状 ------------------------------------------------------------


def test_the_payload_carries_everything_needed_to_recheck_the_calibration() -> None:
    account = BlindSpinAccount()
    spin_blind_rows(700, spin=_spin, measure_rows=lambda: 706, account=account)

    payload = blind_spin_payload(account, rows_to_bot_area=754, source="cli")

    assert payload == {
        "rows_requested": 700,
        "notches_sent": 648,
        "spin_seconds": 1.234,
        "glide_seconds": GLIDE_SETTLE_S,
        "rows_measured": 706,
        "rows_per_notch_observed": round(706 / 648, 3),
        "rows_per_notch_calibrated": ROWS_PER_NOTCH,
        "rows_to_bot_area": 754,
        "source": "cli",
    }


def test_the_calibrated_value_of_the_day_is_stored_alongside_the_observed_one() -> None:
    """⚠️ 日后有人把 1.08 改了，库里这一批老记录才说得清是按哪个数算的。"""
    payload = blind_spin_payload(BlindSpinAccount(), rows_to_bot_area=None, source="default")

    assert payload["rows_per_notch_calibrated"] == ROWS_PER_NOTCH


def test_a_run_that_never_reached_the_bot_area_still_gets_a_record() -> None:
    """没到 bot 区那几趟同样要留痕：`rows_to_bot_area` 记 None，不是省掉这条。"""
    recorded: list[tuple[str, dict[str, object]]] = []
    account = BlindSpinAccount()
    spin_blind_rows(700, spin=_spin, measure_rows=lambda: 706, account=account)

    report_blind_spin(
        account,
        rows_to_bot_area=None,
        source="default",
        record=lambda message, payload: recorded.append((message, payload)),
    )

    assert len(recorded) == 1
    assert recorded[0][1]["rows_to_bot_area"] is None
    assert "700 行" in recorded[0][0] and "648 格" in recorded[0][0]


def test_the_line_says_so_when_the_calibration_could_not_be_measured() -> None:
    """「没测出来」和「测出来正好等于标定」是两种事，正文得分得开。"""
    recorded: list[tuple[str, dict[str, object]]] = []
    account = BlindSpinAccount()
    spin_blind_rows(700, spin=_spin, measure_rows=lambda: None, account=account)

    report_blind_spin(
        account,
        rows_to_bot_area=754,
        source="default",
        record=lambda message, payload: recorded.append((message, payload)),
    )

    assert "没测出" in recorded[0][0]


# -- 慢拖那条回滚路 ------------------------------------------------------------


def test_the_rollback_path_drags_one_screen_per_screenful_of_rows() -> None:
    """⚠️ **这条路存在的唯一理由是回滚。**

    `storage.models.MilitaryAttackConfigRow` 上写着：`blind_scroll_rows` 置空即
    退回慢拖，不需要改代码、不需要重新发版。没有这条路，回滚就变成「改代码 + 发版」。
    """
    drags = 0

    def scroll_blind() -> None:
        nonlocal drags
        drags += 1

    walked = drag_blind_rows(332, scroll_blind=scroll_blind, say_line=lambda _m: None)

    assert drags == round(332 / ROWS_PER_SCROLL)
    assert walked == round(drags * ROWS_PER_SCROLL)


def test_the_rollback_path_says_out_loud_that_it_is_the_old_road() -> None:
    """走的是老路这件事必须说出来：不说的话「怎么还是 5 分钟」就查不出原因。"""
    said: list[str] = []

    drag_blind_rows(332, scroll_blind=lambda: None, say_line=said.append)

    assert any("慢拖" in line for line in said)
