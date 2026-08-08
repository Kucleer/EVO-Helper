"""回放页分节的定位。

参战战舰那张表**高度不固定**（用户确认），回放内容还会滚动，所以行界不能写死。
布局里原来写死 `participating_rows=(405, 750)`，实测在一份真实战报上穿透到了
「第1回合【剩余战舰】」——同一批数量被读两遍，守方合计从 247 变成 144，
而入库流程一路"成功"，没有任何报错。
"""

from __future__ import annotations

from evo_helper.vision.report_layout import (
    BANNER_MIN_HEIGHT,
    banner_bands,
    sections_from_banners,
)

DARK = 40.0
BRIGHT = 200.0


def profile(*, height: int, banners: list[tuple[int, int]]) -> list[float]:
    """造一条逐行亮度：暗背景上放几条亮带。"""
    rows = [DARK] * height
    for start, end in banners:
        for y in range(start, end):
            rows[y] = BRIGHT
    return rows


def test_finds_each_banner() -> None:
    rows = profile(height=400, banners=[(50, 80), (250, 280)])
    assert banner_bands(rows, top=0) == [(50, 79), (250, 279)]


def test_the_top_offset_is_carried_into_the_result() -> None:
    rows = profile(height=400, banners=[(50, 80)])
    assert banner_bands(rows, top=300) == [(350, 379)]


def test_thin_bright_rows_are_not_banners() -> None:
    """行内高亮和面板描边也亮，但它们都薄。"""
    rows = profile(height=400, banners=[(50, 50 + BANNER_MIN_HEIGHT - 1), (250, 280)])
    assert banner_bands(rows, top=0) == [(250, 279)]


def test_a_banner_running_to_the_bottom_still_counts() -> None:
    rows = profile(height=400, banners=[(360, 400)])
    assert banner_bands(rows, top=0) == [(360, 399)]


def test_sections_span_from_one_banner_to_the_next() -> None:
    sections = sections_from_banners([(100, 130), (300, 330)], bottom=500, padding=3)
    assert sections == [(133, 297), (333, 497)]


def test_the_last_section_runs_to_the_bottom() -> None:
    assert sections_from_banners([(100, 130)], bottom=500, padding=3) == [(133, 497)]


def test_a_section_with_no_room_is_dropped() -> None:
    # 两条横幅贴在一起时中间没有内容，报一个空区间只会让下游读到噪声。
    assert sections_from_banners([(100, 130), (132, 160)], bottom=500, padding=3) == [(163, 497)]


def test_no_banners_means_no_sections() -> None:
    assert sections_from_banners([], bottom=500) == []
    assert banner_bands([], top=0) == []


def test_the_measured_replay_splits_participating_from_round_one() -> None:
    """实测 2026-08-08 那份战报：参战区 402–652，第1回合 683–876。

    写死的下界 750 会把 683 之后的回合行一并框进参战区——正是那次
    「17/31/13 各出现两遍、合计 144」的成因。
    """
    bands = [(373, 399), (655, 680)]
    sections = sections_from_banners(bands, bottom=879)
    assert sections[0] == (402, 652)
    assert sections[1] == (683, 876)
    assert sections[0][1] < 750
