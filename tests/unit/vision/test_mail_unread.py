"""信箱列表页「未读 / 已读」的颜色判据 —— 判据、取样，以及「不给答案」那几条。

## 这份用例守的是什么

用户口径（2026-09-08）：「邮箱中未读邮件（前 4 个），字体颜色是与已读邮件不一致的，
你在开未读邮件时，需要把这些内容都阅读了。」

未读是比「列表页时刻」更强的信号：未读 ⇒ 我们还没开过它 ⇒ 库里不可能有它的战报。
阈值已于 **2026-09-08 在实机四屏实拍上标定**（`CALIBRATION`）；实拍图一律不进
Git，所以这里的合成图验的是**通路与取样几何**，**不是「量得准」**——后者只有
实拍能回答，办法在 `tools.mail_unread_probe`。

⚠️ **本文件最要紧的两条仍旧是「不给答案」那两条**：

1. 没标定（换版面之后要撤回的那个状态）⇒ `None`（不许挑一侧倒）；
2. 落在两档之间的空档里 ⇒ `None`（不许把空档收成一条线）。

两条守的是同一件事：这道闸的两侧代价**不对称**。判成未读只是多烧开封预算；
判成已读会让那一封继续和别人抢那 8 封开封预算，一直排到掉出扫描下限（默认 6 小时）
为止——那之后日常那趟**永远翻不到它**（整段在 `pirate_loop.MAIL_UNREAD_MAX_OPENS`）。

⚠️ **第三条同样要紧，而且是这一版加的：取样的几何。** 判据量在**自对齐的标题带**
上（拿每行都有的时刻带当锚点），不是整行、也不是按名义行顶取的标题带。理由是
实测：同一批 27 行样本上，整行只有 1.7× 的分离度，按名义行顶取**离网格就归零**，
自对齐是 40×。合成图那一节按这三件事各钉一条。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.tools.mail_unread_probe import propose_calibration
from evo_helper.vision import mail_unread
from evo_helper.vision.mail_unread import (
    CALIBRATION,
    WARMTH_BUCKET_WIDTH,
    WARMTH_BUCKETS,
    MailRowColor,
    MailUnreadCalibration,
    classify_unread,
)
from evo_helper.vision.report_layout import LIVE_LAYOUT
from support.mailbox import build_mail_list_frame

#: 一组测试专用的标定。**故意不去动 `CALIBRATION`**：那个常量的值本身就是
#: 「有没有标定过」这条事实，测试改掉它就等于把被测事实抹掉了。
TEST_CALIBRATION = MailUnreadCalibration(
    min_warmth=64,
    unread_min_share=0.05,
    read_max_share=0.01,
    measured_on="合成图（只验通路，不是实拍标定）",
)


def _color(share_by_bucket: dict[int, int], *, pixels: int = 1000) -> MailRowColor:
    buckets = [0] * WARMTH_BUCKETS
    for bucket, count in share_by_bucket.items():
        buckets[bucket] = count
    return MailRowColor(pixels=pixels, warmth_buckets=tuple(buckets), mean_luminance=60)


# -- 「不给答案」那两条 -------------------------------------------------------


def test_no_calibration_means_no_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️⚠️ **没标定就交回 `None`，一侧都不许倒。**

    这条是整个改动的安全底座：`None` 在策略层的意思是「按改动之前的行为办」。
    倒向「已读」会让未读邮件一封都开不到（漏数据）；倒向「未读」会让每屏六行
    全进「必开」那条路，把开封预算翻倍烧掉，而日志上看着一切正常。

    ⚠️ **标定落地（2026-09-08）之后这条不但要留着，还更要紧了**：换了游戏版面
    就要把 `CALIBRATION` 撤回 `None` 重采，而那一刻整条链路必须退回改动之前的
    行为。所以这里把常量打成 `None` 来验，而不是靠「它本来就是 None」。
    """
    monkeypatch.setattr(mail_unread, "CALIBRATION", None)

    assert classify_unread(_color({15: 900})) is None
    assert classify_unread(_color({0: 900})) is None


def test_the_live_calibration_says_where_it_was_measured() -> None:
    """⚠️ **标定必须带出处，而且门槛要落在实测的空档里、不许贴着任一侧。**

    实测两侧（2026-09-08 四屏实拍、自对齐标题带）：未读最小 **0.2137**、
    已读最大 **0.0054**。谁哪天把两档往外挪到贴着样本边缘，这条会红——
    那正是「这套配方只会这样错」翻过两次车的形状（`tools.nav_value_corpus`）。
    """
    assert CALIBRATION is not None, "撤回未标定状态时，改这条并说清为什么"
    assert "2026-09-08" in CALIBRATION.measured_on
    assert 0.0054 < CALIBRATION.read_max_share
    assert CALIBRATION.unread_min_share < 0.2137


def test_the_two_measured_sides_land_on_the_right_answers() -> None:
    """把实测的那两个占比原样喂进去：0.2137 ⇒ 未读、0.0054 ⇒ 已读。

    这一条钉的是「那组数**对着它自己的样本**是对的」——阈值挪过头、或者
    `warm_share` 的起始桶算错，两侧就会有一侧倒过来。
    """
    unread_side = _color({13: 214}, pixels=1000)  # 暖占比 0.214
    read_side = _color({0: 995, 1: 5}, pixels=1000)  # 暖占比 0.005

    assert classify_unread(unread_side) is True
    assert classify_unread(read_side) is False


def test_the_gap_between_the_two_bands_is_not_an_answer() -> None:
    """⚠️⚠️ **落在两档之间 ⇒ `None`。判据是一个空档，不是一条线。**

    只有一条线的那一版没有「判不出」这个结局，于是任何一次读偏都会被当成一个
    确定的答案用出去。`OUTCOME_INK_THRESHOLD` 的注释写着同一条：门槛该落在一个
    数量级的空档里。
    """
    # 占比 0.03，落在 read_max_share=0.01 与 unread_min_share=0.05 之间
    middle = _color({5: 30}, pixels=1000)

    assert classify_unread(middle, TEST_CALIBRATION) is None


def test_a_calibration_without_a_gap_is_refused_at_construction() -> None:
    """两档写成同一个数（或反过来）在构造时就该炸，而不是等到实机上。"""
    with pytest.raises(ValueError, match="空档"):
        MailUnreadCalibration(
            min_warmth=64, unread_min_share=0.05, read_max_share=0.05, measured_on="错的"
        )
    with pytest.raises(ValueError, match="空档"):
        MailUnreadCalibration(
            min_warmth=64, unread_min_share=0.01, read_max_share=0.05, measured_on="反了"
        )


# -- 标定之后两侧都要判得出（否则「不给答案」那两条也能被空实现骗过） ---------


def test_a_warm_row_reads_as_unread() -> None:
    assert classify_unread(_color({5: 900}, pixels=1000), TEST_CALIBRATION) is True


def test_a_cold_row_reads_as_read() -> None:
    assert classify_unread(_color({0: 1000}, pixels=1000), TEST_CALIBRATION) is False


def test_a_row_with_no_pixels_reads_as_nothing() -> None:
    """ROI 落在画面外时 `pixels` 是 0。除以它会炸，倒向任一侧都会撒谎。"""
    empty = MailRowColor(pixels=0, warmth_buckets=(0,) * WARMTH_BUCKETS, mean_luminance=0)

    assert classify_unread(empty, TEST_CALIBRATION) is None
    assert empty.warm_share(64) == 0.0


def test_warm_share_counts_the_bucket_that_holds_the_threshold() -> None:
    """按**桶**算，并且往「暖」的一侧倒（与 `MailRow.may_be` 同方向）。

    直方图已经把精度收到一桶宽，这里再假装有像素级精度只会让标定出来的数字
    和复算的结果对不上。
    """
    color = _color({4: 100, 5: 100}, pixels=1000)
    boundary = 4 * WARMTH_BUCKET_WIDTH

    assert color.warm_share(boundary) == pytest.approx(0.2)
    assert color.warm_share(boundary + 1) == pytest.approx(0.2), "桶内不细分"
    assert color.warm_share(boundary + WARMTH_BUCKET_WIDTH) == pytest.approx(0.1)


# -- 标定工具：分不开时必须拒绝出数 -------------------------------------------


def test_the_probe_refuses_to_propose_when_the_two_sides_overlap() -> None:
    """⚠️ **分不开时给的是两组数字，不是一个凑出来的阈值。**

    这一条是标定那条路上唯一的守门人：它一旦肯在重叠的样本上出数，
    下一步就是有人把那组数粘进 `CALIBRATION`。
    """
    same = [_color({5: 100}, pixels=1000)]

    proposal = propose_calibration(same, list(same), measured_on="重叠样本")

    assert proposal.calibration is None
    assert "分不开" in proposal.note


def test_the_probe_needs_both_sides() -> None:
    """只有未读样本时也不许出数：已读那一侧的上界无从得知。"""
    proposal = propose_calibration([_color({5: 100})], [], measured_on="只有一侧")

    assert proposal.calibration is None


def test_the_probe_leaves_a_gap_between_the_two_bands() -> None:
    """分得开时给出的两档之间必须**真的留着空档**，不是贴着样本边缘。"""
    unread = [_color({11: 300}, pixels=1000)]
    read = [_color({0: 1000}, pixels=1000)]

    proposal = propose_calibration(unread, read, measured_on="合成图")

    assert proposal.calibration is not None
    assert proposal.calibration.read_max_share < proposal.calibration.unread_min_share
    # 两侧都要往内让，落差不许被吃光
    assert 0 < proposal.calibration.read_max_share
    assert proposal.calibration.unread_min_share < 0.3


# -- 合成图：像素那一侧量得出来吗 ---------------------------------------------

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")


#: 合成图的画法与那几个颜色常量住在 `support.mailbox`：颜色这一组用例量它的颜色，
#: `test_mail_row_alignment.py` 量它的框。两边共用一张图，几何才不会各自漂。
def _frame(unread_rows: set[int], **kwargs: Any) -> Any:
    return build_mail_list_frame(unread_rows, **kwargs)


def _screens(unread_rows: set[int], **kwargs: Any) -> Any:
    from evo_helper.vision.optional.report_screens import ImageReportScreens

    return ImageReportScreens(_frame(unread_rows, **kwargs), LIVE_LAYOUT)


def test_the_pixel_layer_separates_a_warm_title_from_a_white_one() -> None:
    """⚠️ 这一条验的是**量得出来**，不是量得准。准不准只有实拍能回答。"""
    colors = _screens({0, 1}).mail_row_colors()

    assert len(colors) == LIVE_LAYOUT.mail_visible_rows
    assert all(color is not None for color in colors)
    warm = [color.warm_share(TEST_CALIBRATION.min_warmth) for color in colors]
    assert min(warm[:2]) > TEST_CALIBRATION.unread_min_share
    assert max(warm[2:]) <= TEST_CALIBRATION.read_max_share
    assert [classify_unread(color, TEST_CALIBRATION) for color in colors] == [
        True,
        True,
        False,
        False,
        False,
        False,
    ]


@pytest.mark.parametrize("drift", [0, 11, 38, 60])
def test_the_title_band_follows_the_row_off_the_grid(drift: int) -> None:
    """⚠️⚠️ **取样自对齐：列表离网格之后照样判得出来。**

    这是整个取样改动的理由。实拍四屏量到的漂移是 +11 / +23 / +38 / +82 像素——
    按**名义行顶**取那条 18px 的标题带，对齐那一屏取到 0.233、离网格的两屏取到
    **0.000**，两侧当场分不开。自对齐（拿每行都有的时刻带当锚点）在同一批样本上
    是 **40×**。

    ⚠️ 把 `_title_color` 的锚点改回名义行顶，这一条会红。
    """
    colors = _screens({0, 1}, drift=drift).mail_row_colors()

    assert [classify_unread(color, TEST_CALIBRATION) for color in colors] == [
        True,
        True,
        False,
        False,
        False,
        False,
    ], f"漂移 {drift} 像素之后判据就失效了"


def test_a_row_without_a_time_band_is_not_answered() -> None:
    """⚠️ **定位不到时刻带 ⇒ `None`，绝不回落到名义行顶。**

    回落等于在离网格的那几屏上给出一个确定的错答案，而 `None` 在策略层的意思是
    「按改动之前的行为办」。
    """
    colors = _screens({0, 1}, time_column=False).mail_row_colors()

    assert list(colors) == [None] * LIVE_LAYOUT.mail_visible_rows
    assert [classify_unread(color, TEST_CALIBRATION) for color in colors] == [None] * 6


def test_the_red_envelope_badge_stays_out_of_the_title_band() -> None:
    """⚠️ **`x 792..812` 那个红色角标不许收进来。** 它每一行都有，也是暖色——
    收进来会把已读那一侧整体抬起来，空档当场没了。

    ⚠️ 把 `MAIL_TITLE_COLUMN` 的左界往左挪到 792，这一条会红。
    """
    colors = _screens(set()).mail_row_colors()

    assert all(color is not None and color.warm_share(16) == 0.0 for color in colors)


def test_every_row_is_measured_over_the_whole_title_band() -> None:
    """`pixels` 就是标题带面积：测量这一步**不先挑墨迹**。

    挑墨迹要一个亮度门槛，而那个门槛同样没标定过——用它等于在测量这一步
    先做一次未标定的判断，把标定的自由度提前焊死。
    （自对齐**定位**用的那个门槛不算：它决定「量哪一块」，不是「暖不暖」。）
    """
    from evo_helper.vision.optional.report_screens import (
        MAIL_TITLE_BAND_HEIGHT,
        MAIL_TITLE_COLUMN,
    )

    expected = (MAIL_TITLE_COLUMN[1] - MAIL_TITLE_COLUMN[0]) * MAIL_TITLE_BAND_HEIGHT

    colors = _screens(set()).mail_row_colors()

    assert expected == 72 * 18, "标题带的面积是量出来的 1,296 像素，不是随手取的"
    assert all(color is not None and color.pixels == expected for color in colors)


def test_the_crops_are_wider_than_the_measured_roi() -> None:
    """⚠️ 裁片是**整个名义行**再各边加一圈，不是那条 18px 的读数带。

    读数带是自对齐算出来的，而「自对齐算错了没有」正是事后最要查的一件事——
    只存那条带子等于把要复核的东西提前裁掉。整行裁片里既有标题也有时刻格，
    照 `_mail_time_bands` 的做法能整条复算一遍；大一圈那条理由照旧
    （否则分不出「本来就是白的」和「框切掉了颜色」）。
    """
    from evo_helper.vision.optional.report_screens import MAIL_ROW_EVIDENCE_PAD

    region = LIVE_LAYOUT.mail_row(0)
    crops = _screens({0}).mail_row_crops()

    assert len(crops) == LIVE_LAYOUT.mail_visible_rows
    assert crops[0].width == (region.right - region.left) + 2 * MAIL_ROW_EVIDENCE_PAD
    assert crops[0].height == (region.bottom - region.top) + 2 * MAIL_ROW_EVIDENCE_PAD


def test_the_probe_cli_runs_offline_on_a_gbk_console(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """整条命令行跑一遍：读 JSON → 落 PNG → 出候选。**stdout 挂成 GBK。**

    ⚠️ **这一条是抓到过真 bug 的。** 第一版 `main()` 没调
    `make_console_encoding_safe()`，于是在 Windows 中文控制台上跑到最后一句
    「⚠️ 这是候选，不是结论」时 `UnicodeEncodeError` 当场崩 —— 而前面的表已经
    打完了，**看着像跑成功了**。同一个坑本仓 2026-08-10 在实机上付过一次代价
    （整段记在 `scan_coordinates.say` 与 `test_console_encoding.py` 上）。
    """
    import base64
    import io
    import json
    import sys

    from evo_helper.tools.mail_unread_probe import main

    screens = _screens({0, 1, 2, 3})
    colors = screens.mail_row_colors()

    def _png(image: Any) -> str:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    record = {
        "id": 8891,
        "rows": [
            {
                "index": index,
                "subject": "海盗攻击报告",
                "raw_time_text": None,
                "kind": "PIRATE",
                "unread": None,
                "pixels": color.pixels,
                "warmth_buckets": list(color.warmth_buckets),
                "mean_luminance": color.mean_luminance,
            }
            for index, color in enumerate(colors)
        ],
        "mail_row_png_base64": [_png(crop) for crop in screens.mail_row_crops()],
    }
    logs = tmp_path / "logs.json"
    logs.write_text(json.dumps([record]), encoding="utf-8", newline="\n")
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="gbk"))

    code = main(
        ["--out", str(tmp_path / "corpus"), "--from-file", str(logs), "--unread", "8891:0,1,2,3"]
    )

    assert code == 0
    assert sorted(p.name for p in (tmp_path / "corpus").iterdir()) == [
        f"8891-row{index}.png" for index in range(6)
    ]


def test_the_probe_proposes_a_calibration_from_the_synthetic_frame() -> None:
    """整条标定路径跑通一遍：量 → 标签 → 候选阈值 → 用那组阈值重判。

    ⚠️ 出来的这组数**不是实机标定**（合成图上的颜色是画的）。这条验的是
    「拿到实拍之后照这条路走就能出数」。
    """
    colors = _screens({0, 1, 2, 3}).mail_row_colors()

    proposal = propose_calibration(colors[:4], colors[4:], measured_on="合成图")

    assert proposal.calibration is not None
    assert [classify_unread(color, proposal.calibration) for color in colors] == [
        True,
        True,
        True,
        True,
        False,
        False,
    ]
