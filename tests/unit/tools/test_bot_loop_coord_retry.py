"""坐标核对失败之后要**自愈一次**，并且留下现场。

事故（2026-08-11 00:55–01:08，一次真实的 13 分钟运行）：

    00:55:47 目标 2:320:11（NEEDS_PROBE）
    00:56:19   预设条上找不到 '探路'；这一屏读到的是 []；关掉面板，不打这一发
    00:56:23 目标 2:321:5（NEEDS_PROBE）
    00:56:40   坐标核对不过：面板读作 ':9:320:5'，请求的是 2:321:5
    00:56:58   坐标核对不过：面板读作 ':9:322:16'，请求的是 2:322:16
    ...（一直到 01:08，连续 44 个目标全部核对不过）

画面从第一个目标之后就整体偏了（读数一律多出个 `:9` 前缀）。当时每个目标只试
一次、失败就跳下一个，于是整整 13 分钟一发都没派出去——而日志里只有一行文字，
连张图都没留，事后完全无从判断画面到底成了什么样。

**判据本身不许放松。** 上面第二行读到的是 `2:320:5`——上一个目标的星系，而请求
的是 `2:321:5`。那一次核对拦对了；放松成「位次对上就行」就是往错误的星球扔舰队。

所以这里钉两件事：失败之后会复位画面重试一次；以及存现场，但有封顶。
"""

from __future__ import annotations

from typing import Any

from evo_helper.domain.models import Coordinate
from evo_helper.tools.bot_loop import BotLoop

TARGET = Coordinate(2, 321, 5)


class _Navigator:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def goto(self, coordinate: Coordinate) -> None:
        self._events.append(f"goto {coordinate.system}")


def _loop(events: list[str], verdicts: list[bool]) -> Any:
    loop = BotLoop.__new__(BotLoop)
    loop._navigator = _Navigator(events)  # type: ignore[attr-defined]

    def _confirm(_coordinate: Coordinate) -> bool:
        verdict = verdicts.pop(0)
        events.append(f"核对 {'过' if verdict else '不过'}")
        return verdict

    def _reset() -> None:
        events.append("复位画面")

    loop.is_bot_target = _confirm  # type: ignore[assignment, method-assign]
    loop._reset_to_known_screen = _reset  # type: ignore[assignment, method-assign]
    return loop


def test_a_clean_read_costs_nothing_extra() -> None:
    """第一次就过的话不许多走一趟——稳态是绝大多数情况。"""
    events: list[str] = []
    loop = _loop(events, [True])

    assert loop._goto_confirmed(TARGET) is True
    assert events == ["goto 321", "核对 过"]


def test_a_failed_read_resets_the_screen_and_retries() -> None:
    """这条是本文件的重点：失败之后必须复位并重新导航，而不是直接放弃。"""
    events: list[str] = []
    loop = _loop(events, [False, True])

    assert loop._goto_confirmed(TARGET) is True
    assert events == [
        "goto 321",
        "核对 不过",
        "复位画面",
        "goto 321",
        "核对 过",
    ]


def test_it_gives_up_after_one_retry() -> None:
    """重试一次就够。无限重试会把整轮卡死在一个目标上，比跳过还糟。"""
    events: list[str] = []
    loop = _loop(events, [False, False])

    assert loop._goto_confirmed(TARGET) is False
    assert events.count("复位画面") == 1
    assert events.count("goto 321") == 2


# -- 现场留存 -----------------------------------------------------------------


class _Driver:
    """截屏返回一个占位对象——读数走的是被替掉的 `crop_reader`，不碰真图。"""

    def capture(self) -> Any:
        return object()


def _confirming_loop(monkeypatch: Any, *, confirms: bool) -> tuple[Any, list[str]]:
    """一个 `is_bot_target` 走真实实现、但读数与截屏都被替掉的循环。"""
    dumped: list[str] = []

    class _Panel:
        coordinate_text = ":9:320:5"

        def confirms(self, _requested: str) -> bool:
            return confirms

        @property
        def is_bot(self) -> bool:
            return True

        display_name = "bot_2_321_5"

    monkeypatch.setattr(
        "evo_helper.game.system_navigator.crop_reader", lambda _image, _ocr: lambda *a, **k: ""
    )
    monkeypatch.setattr(
        "evo_helper.vision.scan_reading.read_panel_confirming", lambda _reader, _req: _Panel()
    )

    loop = BotLoop.__new__(BotLoop)
    loop._driver = _Driver()  # type: ignore[attr-defined]
    loop._ocr = None  # type: ignore[attr-defined]
    loop._coord_dumps = 0  # type: ignore[attr-defined]
    loop._dump_frame = lambda name, roi=None: dumped.append(name)  # type: ignore[assignment, method-assign]
    return loop, dumped


def test_a_mismatch_leaves_a_frame_behind(monkeypatch: Any) -> None:
    """一行文字复盘不了画面。核对不过就要有图。"""
    loop, dumped = _confirming_loop(monkeypatch, confirms=False)

    assert loop.is_bot_target(TARGET) is False
    assert dumped == ["bot-coord-mismatch"]


def test_dumps_are_capped(monkeypatch: Any) -> None:
    """连续 44 个目标全失败时不能写出 44 张几乎一样的现场图。"""
    loop, dumped = _confirming_loop(monkeypatch, confirms=False)

    for _ in range(10):
        loop.is_bot_target(TARGET)

    assert len(dumped) == BotLoop.MAX_COORD_DUMPS


def test_a_passing_read_dumps_nothing(monkeypatch: Any) -> None:
    loop, dumped = _confirming_loop(monkeypatch, confirms=True)

    assert loop.is_bot_target(TARGET) is True
    assert dumped == []
