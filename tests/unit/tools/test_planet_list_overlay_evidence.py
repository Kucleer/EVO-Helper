"""行星列表被浮层盖住时留下的证据：要连图一起进库，而且不许刷爆。

⚠️ 这一批来自 2026-08-17 的实机故障。bot 连着多轮一发不派，日志每一轮都是

    行星列表坐标 OCR 全空；tesseract='C:\\Program Files\\Tesseract-OCR\\tesseract.exe'
    行星列表上找不到 4:277:15；逐屏读到的是 [[]]；什么都不点
    切不到出发星球 4:277:15（not_found）；这一轮一发都不派

`_dump_frame("planet-list-unreadable")` 确实把整帧存下来了——**存在 runner 那台
机器的 `var/logs` 下**。排障的人在另一台机器上，取不到；最后是用户手工截了一张
图，才认出画面上盖着的是「太空舱」面板。所以这里照
`scan_coordinates.record_unrecognised_screen` 的路子把缩略图塞进 `payload_json`：
`artifacts` 表存的是路径，而路径只在出事那台机器上有意义。
"""

from __future__ import annotations

from typing import Any

import pytest
from PIL import Image

from evo_helper.tools import pirate_loop


@pytest.fixture
def recorded(monkeypatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        pirate_loop,
        "record_system_log",
        lambda level, source, message, **kwargs: rows.append(
            {"level": level, "source": source, "message": message, **kwargs}
        ),
    )
    monkeypatch.setattr(pirate_loop, "_last_overlay_evidence_at", None)
    return rows


def _frame() -> Image.Image:
    return Image.new("RGB", (1920, 917), "black")


def test_the_evidence_carries_both_the_story_and_a_picture(recorded) -> None:
    """一句话说清因果，一张图说清画面上盖着的是什么。"""
    pirate_loop.record_planet_list_overlay_retry(
        "行星列表读空（逐屏 [[]]）；疑似有浮层盖住导航栏，"
        "已点 4 下关闭键并重开列表；重读还是一行都没有",
        {"target": "4:277:15", "screens_before": [[]], "recovered": False},
        capture=_frame,
        now=lambda: 0.0,
    )

    entry = recorded[0]
    assert entry["level"] == "WARNING"
    assert "浮层" in entry["message"]
    assert entry["payload"]["target"] == "4:277:15"
    assert entry["payload"]["screens_before"] == [[]]
    assert entry["payload"]["thumbnail_png_base64"], "跨机排障要的就是这张图"


def test_only_the_picture_is_rate_limited(recorded) -> None:
    """⚠️ **限流不是省空间，是防刷爆**，但也不许把整条记录扔掉。

    文字每次都写：这一支每出现一次就等于一轮没派，那是必须数得清的。
    图限流：画面卡在一个关不掉的面板上时，不限流就是每轮往库里塞一张。
    """
    clock = iter([0.0, 1.0, pirate_loop.OVERLAY_EVIDENCE_INTERVAL_S + 1.0])
    for _ in range(3):
        pirate_loop.record_planet_list_overlay_retry(
            "读空", {"recovered": False}, capture=_frame, now=lambda: next(clock)
        )

    assert len(recorded) == 3, "文字一条都不许省"
    assert [bool(row["payload"].get("thumbnail_png_base64")) for row in recorded] == [
        True,
        False,
        True,
    ]


def test_a_driver_without_a_screenshot_still_leaves_the_text(recorded) -> None:
    """轻量驱动（尤其单元测试桩）只会点击和等待。诊断路径不许因为配不上图就整条丢掉。"""
    pirate_loop.record_planet_list_overlay_retry(
        "读空", {"recovered": False}, capture=None, now=lambda: 0.0
    )

    assert recorded[0]["message"] == "读空"
    assert "thumbnail_png_base64" not in recorded[0]["payload"]


def test_the_switcher_built_by_the_loop_is_wired_to_this_recorder(recorded) -> None:
    """接线本身也要守住：这一段逻辑在 `game/` 层，落地口在这里，中间断了就白写。"""
    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._driver = type("_D", (), {"capture": lambda self: _frame()})()  # type: ignore[attr-defined]

    switcher = pirate_loop.PirateLoop.planet_switcher(loop)
    switcher.record_evidence("读空", {"recovered": False})

    assert recorded[0]["source"] == "tools.pirate_loop"
    assert recorded[0]["payload"]["thumbnail_png_base64"]
