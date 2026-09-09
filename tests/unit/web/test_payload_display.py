"""`payload_json` 怎么显示：图归图、文字归文字。

⚠️ 这一组来自用户 2026-08-17 报的一个具体现象：系统日志那一列把整页宽度撑到
横向滚动条拉不到头。元凶是 `record_unrecognised_screen` 往 `payload_json` 里塞的
`thumbnail_png_base64`——一张 480px 缩略图的 base64 有几万字符，而那串字符对读
日志的人**没有任何用处**，有用的是那张图本身。
"""

from __future__ import annotations

import base64
import json

from evo_helper.web.display import (
    payload_image,
    payload_image_bytes,
    payload_text,
    screenshot_format,
    screenshot_media_type,
)


def test_the_base64_image_never_reaches_the_text_column() -> None:
    """⚠️ 本组的核心判据：**base64 不许出现在正文里。**

    判据落在「那一大串字符在不在」上，而不是「有没有调用某个函数」——后者换个
    写法就绕过去了，而这一条问的正是把页面撑爆的那件事。
    """
    payload = json.dumps({"capture_size": [1920, 917], "thumbnail_png_base64": "A" * 40_000})

    text = payload_text(payload)

    assert "A" * 40 not in text
    assert "thumbnail_png_base64" not in text
    assert "capture_size" in text, "把图摘掉不等于把整段 payload 丢掉"


def test_the_image_is_handed_over_as_something_a_browser_can_show() -> None:
    """摘出来的图要能直接当 `src` 用，否则「摘出来」等于「丢掉」。"""
    payload = json.dumps({"thumbnail_png_base64": "iVBORw0KGgo"})

    assert payload_image(payload) == "data:image/png;base64,iVBORw0KGgo"


def test_the_image_decodes_back_to_the_bytes_that_went_in() -> None:
    """`GET /system-log/{id}/image` 返回的必须是原图，不是缩过一道的。

    列表页现在只标一个链接（内联进 DOM 是 8 万到 16 万字符），所以这一路是
    「图取得到」的唯一保证。
    """
    raw = b"\x89PNG\r\n\x1a\n" + b"pixels" * 500
    payload = json.dumps({"thumbnail_png_base64": base64.b64encode(raw).decode("ascii")})

    assert payload_image_bytes(payload) == raw


def test_an_unreadable_image_is_none_rather_than_an_exception() -> None:
    """⚠️ 解不开就当没有：这一路是排障页面上的一个链接，
    「点开是 404」比「点开把整页控制台打成 500」好认得多。"""
    assert payload_image_bytes(json.dumps({"thumbnail_png_base64": "不是 base64!!"})) is None
    assert payload_image_bytes("{不是 json") is None
    assert payload_image_bytes("{}") is None
    assert payload_image_bytes(None) is None


def test_a_payload_without_a_picture_offers_none() -> None:
    assert payload_image(json.dumps({"nav_text": ""})) == ""
    assert payload_image("{}") == ""
    assert payload_image(None) == ""


def test_a_payload_that_is_only_a_picture_leaves_no_text_behind() -> None:
    """只有图时正文该是空的——不能剩一个空壳 `{}` 占着一行。"""
    assert payload_text(json.dumps({"thumbnail_png_base64": "x"})) == ""


def test_broken_json_is_shown_as_is_rather_than_swallowed() -> None:
    """⚠️ **解析不出来就原样显示，绝不吞掉。**

    诊断数据宁可显示得难看，也不要因为格式不对而**整条消失**——查故障时最需要
    它的那一刻，往往正是它写坏了的那一刻。
    """
    assert payload_text("{不是 json") == "{不是 json"
    assert payload_image("{不是 json") == ""


def test_an_empty_payload_takes_up_no_room() -> None:
    for blank in (None, "", "{}"):
        assert payload_text(blank) == ""


def test_chinese_survives_the_round_trip() -> None:
    """正文里的中文不能变成 `\\uXXXX`——那是给人读的，不是给机器读的。"""
    assert "认不出" in payload_text(json.dumps({"note": "画面认不出"}, ensure_ascii=False))


def test_a_row_without_a_format_key_is_read_as_png() -> None:
    """⚠️ **这不是兜底，是历史事实。**

    2026-09-09 之前写下的 846 行装的确实是 PNG，而那时还没有
    `thumbnail_image_format` 这个键。改成别的默认值，就等于把那 846 行
    读成打不开的图。
    """
    payload = json.dumps({"thumbnail_png_base64": "AAAA"})

    assert screenshot_format(payload) == "png"
    assert screenshot_media_type(payload) == "image/png"


def test_a_webp_row_says_webp() -> None:
    """2026-09-09 起新写的是 webp，`Content-Type` 要跟着走。"""
    payload = json.dumps({"thumbnail_png_base64": "AAAA", "thumbnail_image_format": "webp"})

    assert screenshot_format(payload) == "webp"
    assert screenshot_media_type(payload) == "image/webp"
    assert payload_image(payload).startswith("data:image/webp;base64,")


def test_a_format_nobody_recognises_falls_back_to_png() -> None:
    """⚠️ **白名单不是洁癖。**

    `payload_json` 是**数据**（runner 写的、行里存的）。把它的字符串直接拼进
    `Content-Type` 或 `data:` URI，等于让一行日志决定响应头。
    认不出的一律回落，不原样透传。
    """
    for bogus in ("svg+xml", "html", "../../etc/passwd", "png; charset=x", ""):
        payload = json.dumps({"thumbnail_image_format": bogus})
        assert screenshot_format(payload) == "png", bogus
        assert screenshot_media_type(payload) == "image/png", bogus


def test_a_format_key_that_is_not_a_string_falls_back_to_png() -> None:
    """写坏的诊断数据不许把这一路打成 500。"""
    for bogus in (1, None, ["webp"], {"webp": True}):
        payload = json.dumps({"thumbnail_image_format": bogus})
        assert screenshot_media_type(payload) == "image/png", repr(bogus)


def test_the_format_is_case_insensitive() -> None:
    """`WEBP` 也认——大小写不该决定图能不能显示。"""
    payload = json.dumps({"thumbnail_image_format": "WEBP"})

    assert screenshot_media_type(payload) == "image/webp"


def test_unreadable_payload_still_answers_png() -> None:
    """整段解不开时也要给一个能用的答案，别抛。"""
    for broken in (None, "", "{}", "not json at all", "[1, 2, 3]"):
        assert screenshot_media_type(broken) == "image/png", repr(broken)
