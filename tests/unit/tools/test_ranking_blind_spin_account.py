"""盲滚那一趟的账：记了什么、折合回多少行，以及慢拖那条回滚路。

⚠️ **这份账是这次改动唯一能事后自证的东西。** `ROWS_PER_NOTCH`(1.08) 2026-08-22 被
实机证伪：10 个样本落在 0.49–1.25、中位 0.96。闭环之后它不再决定这一趟走多远
（走多远是**量**出来的），但「这个数还值不值得当第一轮的猜测」「这一趟收敛了没有」
仍旧只有靠每趟记账才答得出——而没收敛是**静默**的：不报错、不少一条日志，
只是采回来的数少一截。

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


def _spin(
    rows: int,
    *,
    notches: int | None = None,
    seconds: float = 1.234,
    measured: int | None = None,
    rounds: int = 1,
    rates: tuple[float, ...] = (),
) -> SpinResult:
    """一趟假盲滚。`notches` 不给就按第一轮那个猜测算，和真的那一层一致。

    `measured=None` 演的是**开环退路**（起点名次读不出来，这一趟没有闭环保护）。
    """
    return SpinResult(
        rows_requested=rows,
        notches=round(rows / ROWS_PER_NOTCH) if notches is None else notches,
        spin_seconds=seconds,
        rows_measured=measured,
        rounds=rounds,
        rates=rates,
    )


# -- 走过的行数：量出来的优先 --------------------------------------------------


def test_the_rows_walked_are_the_measured_ones() -> None:
    """⚠️ **量出来的那个数优先，绝不拿格数换算去冒充它。**

    闭环那一层每轮都要读一次名次才知道要不要补拨，所以「走了多少行」在那儿就是
    测量值。这里再乘一次标定，误差会一路带到「实测多少行到达 bot 区」上，
    而那正是自标定的输入。
    """
    walk = spin_blind_rows(500, spin=lambda rows: _spin(rows, notches=463, measured=488, rounds=2))

    assert walk.rows == 488
    assert walk.measured is True


def test_the_open_loop_fallback_falls_back_to_the_calibration_and_says_so() -> None:
    """起点读不出来那一支没有测量值，只能乘标定——**而它必须标明自己没量过**。

    原先这里返回一个裸的 `int`，日志照着它打「实走约 700 行」；2026-08-22 实机量到
    真实速率在 0.49–1.25 之间抽，同一句话可能对应 320 行，也可能对应 810 行。
    """
    walk = spin_blind_rows(500, spin=lambda rows: _spin(rows, notches=231, measured=None))

    assert walk.rows == round(231 * ROWS_PER_NOTCH)
    assert walk.measured is False


# -- 记了哪些账 ----------------------------------------------------------------


def test_the_account_carries_both_the_notches_and_the_requested_rows() -> None:
    """⚠️ **两个都要留。** 格数是真发生的事，行数是请求值；只留一个就把
    「要走多少」和「真拨了多少」抹平了，而这条日志存在的意义就是让它们能对上。
    """
    account = BlindSpinAccount()

    spin_blind_rows(700, spin=lambda rows: _spin(rows, seconds=10.5, measured=694), account=account)

    assert account.rows_requested == 700
    assert account.notches_sent == 648
    assert account.spin_seconds == 10.5
    assert account.rows_measured == 694
    assert account.glide_seconds == GLIDE_SETTLE_S


def test_the_glide_wait_is_counted_once_per_round() -> None:
    """闭环每一轮拨完都要等一次滑行才准读——账上就得是轮数 × 一次。

    不等就是在移动中的画面上逐行裁剪，名字横跨两行、名次读不出，
    于是那一轮的测量作废；而作废的表现看着像「OCR 坏了」。
    """
    account = BlindSpinAccount()

    spin_blind_rows(700, spin=lambda rows: _spin(rows, measured=690, rounds=3), account=account)

    assert account.rounds == 3
    assert account.glide_seconds == round(GLIDE_SETTLE_S * 3, 3)


def test_zero_notches_means_no_glide_wait_was_made() -> None:
    """一格都没拨就一次都不等——账上也不许写成等过。"""
    account = BlindSpinAccount()

    walk = spin_blind_rows(0, spin=lambda rows: _spin(rows, notches=0, rounds=0), account=account)

    assert walk.rows == 0
    assert account.glide_seconds == 0.0
    assert account.rows_measured is None


def test_a_run_without_a_measurement_is_recorded_as_unknown() -> None:
    """⚠️ **测不出不是失败，但也不许被抹成一个数。**

    起点名次读不出来时闭环整个用不上，那一趟退回开环一次性拨完。它照样走完、
    照样把距离交给检测段接手，只是这一趟**没有闭环保护**——库里得看得出这件事。
    """
    account = BlindSpinAccount()

    walk = spin_blind_rows(500, spin=lambda rows: _spin(rows, measured=None), account=account)

    assert walk.rows > 0, "测不出名次不影响这一趟走过的距离"
    assert walk.measured is False
    assert account.rows_measured is None
    assert account.rows_per_notch_observed is None


# -- 要害：实测每格走了多少行 --------------------------------------------------


def test_the_observed_rows_per_notch_is_measured_over_the_notches_sent() -> None:
    """实测每格行数 = 实测走了多少行 ÷ 实发格数。**分母是格数，不是行数。**

    拿行数当分母就成了「实测 ÷ 请求」，那量的是「请求走到了没有」而不是
    「一格能走多远」，而后者才是这条日志要回答的问题。
    """
    account = BlindSpinAccount()

    spin_blind_rows(500, spin=lambda rows: _spin(rows, notches=463, measured=500), account=account)

    assert account.rows_per_notch_observed == round(500 / 463, 3)


def test_an_observed_calibration_that_drifted_shows_up_as_a_different_number() -> None:
    """标定漂了就得看得出来——这条日志存在的全部理由。

    实发 463 格却只走了 400 行 = 每格 0.864 行，比第一轮那个猜测（1.08）少两成。
    闭环会把差额补回来，所以这一趟仍旧走够了；但「那个猜测越来越不准」这件事
    只有靠这个数看得出来。
    """
    account = BlindSpinAccount()

    spin_blind_rows(500, spin=lambda rows: _spin(rows, notches=463, measured=400), account=account)

    assert account.rows_per_notch_observed is not None
    assert account.rows_per_notch_observed < ROWS_PER_NOTCH * 0.9


# -- payload 的形状 ------------------------------------------------------------


def test_the_payload_carries_everything_needed_to_recheck_the_calibration() -> None:
    account = BlindSpinAccount()
    spin_blind_rows(
        700,
        spin=lambda rows: _spin(rows, measured=706, rounds=2, rates=(0.55, 1.2345)),
        account=account,
    )

    payload = blind_spin_payload(account, rows_to_bot_area=754, source="cli")

    assert payload == {
        "rows_requested": 700,
        "notches_sent": 648,
        "spin_seconds": 1.234,
        "glide_seconds": round(GLIDE_SETTLE_S * 2, 3),
        "rows_measured": 706,
        "rows_per_notch_observed": round(706 / 648, 3),
        "rows_per_notch_calibrated": ROWS_PER_NOTCH,
        "rounds": 2,
        "rows_per_notch_by_round": [0.55, 1.234],
        "rows_to_bot_area": 754,
        "source": "cli",
    }


def test_the_spread_within_one_run_is_kept_round_by_round() -> None:
    """⚠️ **别把每轮的速率压成一个平均数。**

    推翻开环的那份证据就是**散布**（0.49–1.25，同一趟里也抖），而平均数正好把它
    抹掉。逐轮留着，日后才答得出「这一趟是稳的还是每轮都在跳」。
    """
    account = BlindSpinAccount()
    spin_blind_rows(
        700,
        spin=lambda rows: _spin(rows, measured=700, rounds=3, rates=(0.49, 1.25, 0.9)),
        account=account,
    )

    payload = blind_spin_payload(account, rows_to_bot_area=None, source="cli")

    assert payload["rows_per_notch_by_round"] == [0.49, 1.25, 0.9]


def test_the_calibrated_value_of_the_day_is_stored_alongside_the_observed_one() -> None:
    """⚠️ 日后有人把 1.08 改了，库里这一批老记录才说得清是按哪个数算的。"""
    payload = blind_spin_payload(BlindSpinAccount(), rows_to_bot_area=None, source="default")

    assert payload["rows_per_notch_calibrated"] == ROWS_PER_NOTCH


def test_a_run_that_never_reached_the_bot_area_still_gets_a_record() -> None:
    """没到 bot 区那几趟同样要留痕：`rows_to_bot_area` 记 None，不是省掉这条。"""
    recorded: list[tuple[str, dict[str, object]]] = []
    account = BlindSpinAccount()
    spin_blind_rows(700, spin=lambda rows: _spin(rows, measured=706), account=account)

    report_blind_spin(
        account,
        rows_to_bot_area=None,
        source="default",
        record=lambda message, payload: recorded.append((message, payload)),
    )

    assert len(recorded) == 1
    assert recorded[0][1]["rows_to_bot_area"] is None
    assert "700 行" in recorded[0][0] and "648 格" in recorded[0][0]


def test_the_line_separates_the_request_from_the_measurement() -> None:
    """⚠️ **正文里「请求多少行」和「实测走了多少行」必须是两个数。**

    原先那句话只有一个数（请求值），而「实走约 N 行」是拿格数乘标定算出来的——
    读起来像证据，其实是推算。
    """
    recorded: list[tuple[str, dict[str, object]]] = []
    account = BlindSpinAccount()
    spin_blind_rows(700, spin=lambda rows: _spin(rows, measured=641), account=account)

    report_blind_spin(
        account,
        rows_to_bot_area=754,
        source="default",
        record=lambda message, payload: recorded.append((message, payload)),
    )

    assert "请求 700 行" in recorded[0][0]
    assert "实测走了 641 行" in recorded[0][0]


def test_the_line_says_so_when_the_walk_could_not_be_measured() -> None:
    """「没测出来」和「测出来正好等于请求值」是两种事，正文得分得开。"""
    recorded: list[tuple[str, dict[str, object]]] = []
    account = BlindSpinAccount()
    spin_blind_rows(700, spin=lambda rows: _spin(rows, measured=None), account=account)

    report_blind_spin(
        account,
        rows_to_bot_area=754,
        source="default",
        record=lambda message, payload: recorded.append((message, payload)),
    )

    assert "没测出" in recorded[0][0]
    assert "开环" in recorded[0][0], "这一趟没有闭环保护，日志上得看得出来"


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

    walk = drag_blind_rows(332, scroll_blind=scroll_blind, say_line=lambda _m: None)

    assert drags == round(332 / ROWS_PER_SCROLL)
    assert walk.rows == round(drags * ROWS_PER_SCROLL)
    assert walk.measured is False, "老路一屏都不读，走了多少行全是乘出来的"


def test_the_rollback_path_says_out_loud_that_it_is_the_old_road() -> None:
    """走的是老路这件事必须说出来：不说的话「怎么还是 5 分钟」就查不出原因。"""
    said: list[str] = []

    drag_blind_rows(332, scroll_blind=lambda: None, say_line=said.append)

    assert any("慢拖" in line for line in said)
