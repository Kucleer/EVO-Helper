"""底部导航条：几何、词表、按文本定位。

这条条**可以横向滚动**，而军力榜链和攻击链都点它。2026-08-24 生产事故：攻击链点的
是写死的像素 `(840, 862)`，条被军力榜拖到右段之后那底下变成了**太空舱**（用户实机
确认），于是「行星列表坐标 OCR 全空」当天出现 25 次、每次都以「这一轮一发都不派」收场。
"""

from __future__ import annotations

from evo_helper.domain.text import snap_to_vocabulary
from evo_helper.game.nav_bar import (
    FLEET_LABEL,
    NAV_LABEL_MAX_DISTANCE,
    NAV_LABEL_ROI,
    NAV_LABELS,
    PLANET_LABEL,
    RIGHT_SEGMENT_LABELS,
    label_x,
    merged_labels,
)

#: 拖之前那一屏的实测词框（2026-08-14 实机）。`太空舱` 被 tesseract 切成两个词。
BASELINE = [
    (839, "行星"),
    (920, "舰队"),
    (993, "太空"),
    (1017, "舱"),
    (1081, "商店"),
    (1161, "联盟"),
]

#: 拖之后那一屏。
RIGHT_SEGMENT = [(830, "太空舱"), (918, "商店"), (998, "联盟"), (1079, "排名"), (1160, "设置")]


def _edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, start=1):
        current = [i]
        for j, b in enumerate(right, start=1):
            current.append(
                previous[j - 1] if a == b else 1 + min(previous[j - 1], previous[j], current[j - 1])
            )
        previous = current
    return previous[-1]


# -- 词表 ----------------------------------------------------------------------


def test_every_pair_in_the_vocabulary_is_further_apart_than_the_tolerance() -> None:
    """⚠️⚠️ **词表里任意两项的编辑距离都必须大于 `NAV_LABEL_MAX_DISTANCE`。**

    `snap_to_vocabulary` 靠「唯一命中」判对错：容差之内只有一个候选才算认出来，
    两个并列就判歧义、返回 `None`（宁可认不出去重读，也不要把「设置」认成「排名」）。
    而这条性质只在**词表两两距离足够远**时成立 —— 距离等于容差的两项会让某些误读同时
    命中两个，那时判歧义还算安全；但**距离小于容差**时，一次误读可能唯一命中错的那个。

    2026-08-24 把词表从五个扩成七个（加了「行星」「舰队」）时，这条性质是靠手算确认的
    ——手算不会在下一个人加词时再跑一遍。这条用例会。

    将来谁加一个「行动」（离「行星」编辑距离 1）进来，它当场红。
    """
    for i, first in enumerate(NAV_LABELS):
        for second in NAV_LABELS[i + 1 :]:
            distance = _edit_distance(first, second)
            assert distance > NAV_LABEL_MAX_DISTANCE, (
                f"「{first}」和「{second}」只差 {distance}，而容差是 {NAV_LABEL_MAX_DISTANCE}"
            )


def test_the_vocabulary_covers_both_segments_of_the_bar() -> None:
    """⚠️ 词表必须覆盖**整条条**，不能只有拖完之后那一段。

    2026-08-24 之前它只有右段那五个，于是攻击链要找的「行星」「舰队」贴不回词表
    —— 而那两个正是判断「条在哪一段」唯一可靠的凭据。
    """
    assert PLANET_LABEL in NAV_LABELS
    assert FLEET_LABEL in NAV_LABELS
    for label in RIGHT_SEGMENT_LABELS:
        assert label in NAV_LABELS


def test_the_shop_label_cannot_tell_the_two_segments_apart() -> None:
    """⚠️ **「商店」两段都有，拿它判不出条在哪儿。**

    钉住这条是因为它是选判据时最容易踩的坑：读到「商店」看着像是读到了导航条、
    一切正常，而它在基线段是 1081、在右段是 918 —— 两个位置差 163px，
    点下去是两回事。能当判据的只有各段独有的那几个。
    """
    assert label_x(BASELINE, "商店") == 1081
    assert label_x(RIGHT_SEGMENT, "商店") == 918


# -- 按文本定位 ------------------------------------------------------------------


def test_the_planet_and_fleet_labels_are_found_on_the_baseline() -> None:
    """基线段上读得出「行星」「舰队」，而且 x 与标定像素对得上。

    `pirate_ui.NAV_PLANET = (840, 862)` / `NAV_FLEET = (920, 862)` 就是照这一屏
    标的 —— 条没被挪动时，按文本定位和写死的像素应该给出同一个答案。
    """
    assert label_x(BASELINE, PLANET_LABEL) == 839
    assert label_x(BASELINE, FLEET_LABEL) == 920


def test_neither_is_found_once_the_bar_has_been_dragged_left() -> None:
    """⚠️⚠️ **条被拖到右段之后，「行星」「舰队」一个都读不到。**

    这就是 2026-08-24 事故的判据本身：读不到它们 = 不能点写死的那两个像素。
    旧代码没有这一步，直接点 (840, 862)，而那底下此时是太空舱。
    """
    assert label_x(RIGHT_SEGMENT, PLANET_LABEL) is None
    assert label_x(RIGHT_SEGMENT, FLEET_LABEL) is None


def test_the_ranking_label_only_shows_up_on_the_right_segment() -> None:
    """反过来：「排名」只在右段 —— 军力榜那条链依赖的就是这一点，不许被扩词表改坏。"""
    assert label_x(BASELINE, "排名") is None
    assert label_x(RIGHT_SEGMENT, "排名") == 1079


def test_a_word_outside_the_label_roi_is_never_a_candidate() -> None:
    """⚠️ 落在标签行 ROI 之外的 x 一律不当候选。

    这道闸眼下**打不着**（x 是从 ROI 裁出来的图上换算回来的，出不了界）。留着它是因为
    ROI 和「用什么坐标去点」是两件各自会变的事：哪天有人改成整窗 OCR，它就是唯一还
    站着的东西。**不要因为「测试构造不出真实场景」删掉它。**
    """
    assert label_x([(NAV_LABEL_ROI[0] - 1, "行星")], PLANET_LABEL) is None
    assert label_x([(NAV_LABEL_ROI[2] + 1, "行星")], PLANET_LABEL) is None


def test_a_misread_within_one_edit_still_snaps_home() -> None:
    """差一个字照样认得出 —— 实机上 `chi_sim` 把中文认错一个字是常态。"""
    assert label_x([(839, "行生")], PLANET_LABEL) == 839


def test_a_misread_that_lands_between_two_labels_is_refused() -> None:
    """⚠️ 并列时**判不出来**，不是猜一个。

    「行名」离「行星」和「排名」各 1，两个候选并列 → `snap_to_vocabulary` 返回 `None`
    → 这里也答 `None`。方向是「宁可认不出去重读」，而不是「挑一个点下去」。
    """
    assert snap_to_vocabulary("行名", NAV_LABELS, max_distance=NAV_LABEL_MAX_DISTANCE) is None
    assert label_x([(839, "行名")], PLANET_LABEL) is None


# -- 词框合并 --------------------------------------------------------------------


def test_a_three_character_label_split_by_tesseract_is_merged_back() -> None:
    """`太空舱` 被切成「太空」+「舱」时要合回去，否则贴不回词表。"""
    assert (1005, "太空舱") in merged_labels([(993, "太空"), (1017, "舱")])


def test_two_neighbouring_labels_are_not_merged_into_one() -> None:
    """⚠️ 相邻的两个**导航项**不许被合并。

    实测项距 80、字距最大 24，阈值 40 落在两者之间。按项距 80 挑阈值会把
    「联盟」和「排名」合成一个 `联盟排名`，贴不回词表，于是「排名」永远找不到。
    """
    merged = merged_labels([(1081, "商店"), (1161, "联盟")])

    assert [text for _x, text in merged] == ["商店", "联盟"]
