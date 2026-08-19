"""稀有资源图标：运行时从库里的战报面板上切一次，抠透明底，缓存起来。

面板是合成的，不是实拍：这几条断言守的是**抠底的算法**（从四边漫水、连通、
容差 10），而那件事和真实图标长什么样无关。⚠️ 「切出来的图标好不好看」这件事
本文件守不住，只有对着实拍面板看才知道。
"""

from __future__ import annotations

import io

import pytest

from evo_helper.web import resource_icons
from evo_helper.web.resource_icons import (
    FLOOD_TOLERANCE,
    ICON_CROPS,
    PANEL_SIZE,
    ResourceIconCache,
    cut_icon,
)

Image = pytest.importorskip("PIL.Image", reason="抠图要 Pillow（`[dev]` / `[vision]` 里有）")

#: 面板底色。图标四周就是这个颜色。
BACKGROUND = (18, 24, 38)
#: 图标本体的亮色。
INK = (220, 200, 120)
#: 图标**内部**的一块暗色，和底色只差 4——在容差之内。
#:
#: ⚠️ 它是这一份里最要紧的一块像素：不带连通性地「把所有接近底色的像素刷透明」
#: 会把它一起掏掉，而那种失败在 36×30 的小图上看着只是「这个图标有点花」。
INNER_DARK = (22, 28, 42)


def _panel(size: tuple[int, int] = PANEL_SIZE) -> bytes:
    """合成一张战报面板：整块底色，三个稀有槽位各画一个带暗心的方块。"""
    image = Image.new("RGB", size, BACKGROUND)
    pixels = image.load()
    assert pixels is not None
    for left, top, right, bottom in ICON_CROPS.values():
        for x in range(left + 4, right - 4):
            for y in range(top + 4, bottom - 4):
                pixels[x, y] = INK
        # 图标正中间那一块暗色，四周被 INK 完全包住。
        for x in range(left + 12, left + 18):
            for y in range(top + 10, top + 16):
                pixels[x, y] = INNER_DARK
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", lossless=True)
    return buffer.getvalue()


def _decode(icon_bytes: bytes) -> object:
    return Image.open(io.BytesIO(icon_bytes)).convert("RGBA")


def test_the_icon_is_cut_at_the_calibrated_box() -> None:
    """裁切框是标定常量，切出来的尺寸只能是它。"""
    left, top, right, bottom = ICON_CROPS[5]

    icon = cut_icon(_panel(), 5)

    assert icon is not None
    assert _decode(icon).size == (right - left, bottom - top)  # type: ignore[attr-defined]


def test_the_border_is_flooded_transparent() -> None:
    """四边的底色要没了——否则页面上是一个带底色方块的图标。"""
    icon = _decode(cut_icon(_panel(), 5) or b"")
    pixels = icon.load()  # type: ignore[attr-defined]
    width, height = icon.size  # type: ignore[attr-defined]

    for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        assert pixels[point][3] == 0


def test_the_icon_body_survives() -> None:
    icon = _decode(cut_icon(_panel(), 5) or b"")
    pixels = icon.load()  # type: ignore[attr-defined]
    width, height = icon.size  # type: ignore[attr-defined]

    assert pixels[width // 2, height // 2][3] == 255


def test_a_dark_patch_inside_the_icon_is_not_hollowed_out() -> None:
    """⚠️ **必须从四边进、必须连通**（也就是「漫水」不是「按颜色全选」）。

    图标里本来就有暗色区域，而它们和底色的差值在容差之内。连通性正是把
    「外面的底」和「里面的暗」分开的那个条件；少了它，图标会被掏出洞来。
    """
    left, top, _, _ = ICON_CROPS[5]
    icon = _decode(cut_icon(_panel(), 5) or b"")
    pixels = icon.load()  # type: ignore[attr-defined]

    # 面板坐标 (left+14, top+12) 换算到图标坐标就是 (14, 12)。
    assert pixels[14, 12][3] == 255


def test_the_tolerance_stays_at_the_calibrated_value() -> None:
    """⚠️ 用户实测：放到 16 以上会顺着图标内部的暗缝漏进去，把图标掏出洞来。

    这是**标定常量，不是偏好项**——改它不会让结果更适合谁，只会让结果错。
    """
    assert FLOOD_TOLERANCE == 10


def test_a_panel_of_another_size_is_refused_rather_than_cropped_blindly() -> None:
    """版面漂了的时候，宁可没有图标，也不要在页面上摆三块切歪了的像素。"""
    assert cut_icon(_panel((640, 800)), 5) is None


def test_an_unknown_slot_has_no_calibrated_box() -> None:
    assert cut_icon(_panel(), 0) is None


def test_undecodable_bytes_do_not_raise() -> None:
    """一个装饰性的小图不该有能力把整页变成 500。"""
    assert cut_icon(b"not an image", 5) is None


def test_without_pillow_there_are_simply_no_icons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pillow 是可选依赖，控制台那台机器不一定装。

    缺了就没有图标，**不是**控制台起不来（CLAUDE.md：可选依赖缺失时降级运行）。
    """
    monkeypatch.setattr(resource_icons, "_pillow", lambda: None)

    assert cut_icon(_panel(), 5) is None


def test_the_cache_reads_the_panel_once_even_across_slots() -> None:
    """这一页每几秒刷新一次；每轮去库里捞一张 40KB 的 WEBP 是白烧。"""
    calls = 0

    def load() -> bytes:
        nonlocal calls
        calls += 1
        return _panel()

    cache = ResourceIconCache(load)
    for _ in range(3):
        for slot in ICON_CROPS:
            assert cache.icon(slot) is not None

    assert calls == 1


def test_the_cache_remembers_that_there_was_no_panel() -> None:
    """失败也缓存：不然每一次轮询都会去库里捞一次、再失败一次。"""
    calls = 0

    def load() -> bytes | None:
        nonlocal calls
        calls += 1
        return None

    cache = ResourceIconCache(load)
    assert cache.icon(5) is None
    assert cache.icon(8) is None

    assert calls == 1


def test_the_icons_are_png_so_they_can_carry_transparency() -> None:
    icon = ResourceIconCache(_panel).icon(9)

    assert icon is not None
    assert icon.media_type == "image/png"
    assert icon.image_bytes.startswith(b"\x89PNG")


def test_the_three_calibrated_boxes_are_the_rare_slots() -> None:
    """5 = 合金碎片、8 = 泰坦立方、9 = 收割者碎片。"""
    from evo_helper.domain.overview import RARE_SLOTS

    assert tuple(sorted(ICON_CROPS)) == RARE_SLOTS
