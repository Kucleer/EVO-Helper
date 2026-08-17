"""`payload_json` 怎么显示：图归图、文字归文字。

⚠️ 这一组来自用户 2026-08-17 报的一个具体现象：系统日志那一列把整页宽度撑到
横向滚动条拉不到头。元凶是 `record_unrecognised_screen` 往 `payload_json` 里塞的
`thumbnail_png_base64`——一张 480px 缩略图的 base64 有几万字符，而那串字符对读
日志的人**没有任何用处**，有用的是那张图本身。
"""

from __future__ import annotations

import json

from evo_helper.web.display import payload_image, payload_text


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
