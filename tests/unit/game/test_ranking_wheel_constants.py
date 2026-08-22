"""滚轮盲滚的标定常量。这些数一旦漂了，盲滚距离会**静默**变化。

钉死取值而不是「大于 0 就行」是有意的：这四个数里三个的失效方式都是
「事件发出去了、列表没走」，日志上看不出任何异常。写死了才有人在改动时被拦一下。
"""

from evo_helper.game import ranking_ui


def test_one_notch_is_the_windows_standard_delta() -> None:
    # ⚠️ 一格 = 120。发不足一格等于没发（实测 dwData=-1 时 80 格只走 0-3 行）；
    # 一个事件发大 delta 会被游戏封顶（实测 800 格只走 14px）。两侧都静默。
    assert ranking_ui.WHEEL_DELTA == 120


def test_notch_gap_matches_the_measured_human_cadence() -> None:
    # 16ms 是用户手动连滚的间隔中位数。拉到 117ms/格（pyautogui.PAUSE 的默认值）
    # 就攒不起动量，实测 80 格只走 2 行。
    assert ranking_ui.WHEEL_GAP_S == 0.016


def test_rows_per_notch_is_the_measured_calibration() -> None:
    assert ranking_ui.ROWS_PER_NOTCH == 1.08


def test_blind_rows_default_is_the_user_configured_value() -> None:
    assert ranking_ui.BLIND_SCROLL_ROWS == 700


def test_glide_settle_covers_the_measured_inertia() -> None:
    # 实测滑行 1.6-2.3 秒才停；取 2.5 留余量。这里比下界而不是等号：
    # 往上调（等更久）永远是安全方向，往下调会让检测段在移动中的画面上读行。
    assert ranking_ui.GLIDE_SETTLE_S >= 2.3


def test_blind_margin_rows_is_derived_from_the_screen_margin_not_written_down() -> None:
    # ⚠️ 钉的是**算式**，不是那个数。写死一个数就会和它的来历分岔：
    # 日后谁重新标定了 `ROWS_PER_SCROLL`，余量必须跟着走。
    assert ranking_ui.BLIND_SCROLL_MARGIN_ROWS == round(
        ranking_ui.BLIND_SCROLL_MARGIN * ranking_ui.ROWS_PER_SCROLL
    )


def test_blind_margin_rows_stays_above_the_measured_noise_span() -> None:
    # 余量偏小是偏危险的一侧（余量越小，自动标定给出的盲滚行数越大）。
    # 屏口径下实测噪声跨度是 6 屏，折合 44-50 行——余量必须明显在它之上。
    assert ranking_ui.BLIND_SCROLL_MARGIN_ROWS > 50


def test_new_constants_are_exported() -> None:
    for name in (
        "WHEEL_DELTA",
        "WHEEL_GAP_S",
        "ROWS_PER_NOTCH",
        "GLIDE_SETTLE_S",
        "BLIND_SCROLL_ROWS",
        "BLIND_SCROLL_MARGIN_ROWS",
    ):
        assert name in ranking_ui.__all__
