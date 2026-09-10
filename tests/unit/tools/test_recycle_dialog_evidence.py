"""残骸框没弹出来时，要留下**原分辨率**的证据。

## 为什么

2026-09-11 04:43 实机：`5:263:12` 的回收按钮**找到了也点了**，可标题读到
`'UF HK'` —— 拉丁乱码，离「回收残骸」要多远有多远，`looks_like_recycle_dialog`
的模糊匹配再放宽也够不着。作业结成 `screen_error`，日志里只剩这五个字符。

光凭这个字符串**分不出三种毛病**，而三者的善后完全相反：

    框还没画完   → 该加重试次数 / 拉长间隔
    ROI 落偏了   → 该改 RECYCLE_DIALOG_TITLE_ROI
    点开了别的窗口 → 该改按钮定位

这正是「没有回收按钮」那条支线在 `#309` 之前的处境 —— 那次我连猜三个原因
**全错**，是取证图一上来就否掉的。所以这一条不去改判据，只加取证。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from evo_helper.game import pirate_ui
from evo_helper.tools.bot_loop import BotLoop

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")


# -- 几何 ------------------------------------------------------------------------


def test_the_evidence_roi_strictly_contains_the_reading_roi() -> None:
    """⚠️ **取证框必须真的把读字框整个包住，而且四边都要更大。**

    取证要回答的是「框到底在不在、标题画在哪」。要是取证框和读字框一样大，
    那么「框整个没画出来」和「标题挪了 20px」裁出来一模一样 —— 白存。
    """
    ex0, ey0, ex1, ey1 = BotLoop.RECYCLE_DIALOG_EVIDENCE_ROI
    tx0, ty0, tx1, ty1 = pirate_ui.RECYCLE_DIALOG_TITLE_ROI

    assert ex0 < tx0 and ey0 < ty0, "取证框的左上角必须在读字框之外"
    assert ex1 > tx1 and ey1 > ty1, "取证框的右下角必须在读字框之外"


def test_the_evidence_roi_reaches_the_dialog_buttons() -> None:
    """⚠️ 下边界要够到 583 那一行（绿✓ / 红✗）。

    「框在不在」最硬的证据是那两个按钮在不在，比标题的字形可信 ——
    同 [[locate-controls-by-label-not-coordinates]] 那条口径。
    """
    _, _, _, ey1 = BotLoop.RECYCLE_DIALOG_EVIDENCE_ROI
    assert ey1 > pirate_ui.RECYCLE_DIALOG_CONFIRM[1], "取证框够不到绿✓ 那一行，判不出框在不在"


# -- 取证本身 --------------------------------------------------------------------


def _loop(frame: Any) -> BotLoop:
    """真的 `BotLoop` 实例，只是不跑 `__init__`。

    ⚠️ 不写替身类：替身要自己抄一份 ROI 和限流常量，抄完就会跟真类飘移 ——
    那样这些用例守的就不再是生产代码里的那组值了。
    """
    loop = object.__new__(BotLoop)
    loop._frame_reader = lambda: (frame, None)  # type: ignore[method-assign]
    return loop


def _record(
    monkeypatch: pytest.MonkeyPatch, title: str, *, frame: Any = None
) -> list[dict[str, Any]]:
    from evo_helper.tools import bot_loop as module

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        module,
        "record_system_log",
        lambda _lvl, _src, _msg, payload=None: seen.append(payload or {}),
    )
    BotLoop._last_dialog_evidence_at = None
    if frame is None:
        frame = Image.new("RGB", (1920, 917), (17, 23, 34))
    _loop(frame)._record_recycle_dialog_evidence(title)
    return seen


def test_the_crop_comes_out_at_full_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 一个像素都不许缩 —— 480 宽缩略图上标题就是一团糊斑。"""
    import base64
    import io

    seen = _record(monkeypatch, "UF HK")

    assert len(seen) == 1
    payload = seen[0]
    assert payload["dialog_title_raw"] == "UF HK"
    x0, y0, x1, y1 = BotLoop.RECYCLE_DIALOG_EVIDENCE_ROI
    crop = Image.open(io.BytesIO(base64.b64decode(payload["dialog_png_base64"])))
    assert crop.size == (x1 - x0, y1 - y0)


def test_evidence_never_raises_when_the_frame_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取不到帧就只交文字，**不抛** —— 取证不许把链路弄死。"""
    from evo_helper.tools import bot_loop as module

    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        module,
        "record_system_log",
        lambda _lvl, _src, _msg, payload=None: seen.append(payload or {}),
    )

    def _no_frame() -> tuple[Any, Any]:
        raise RuntimeError("截不到图")

    BotLoop._last_dialog_evidence_at = None
    loop = object.__new__(BotLoop)
    loop._frame_reader = _no_frame  # type: ignore[method-assign]
    loop._record_recycle_dialog_evidence("UF HK")

    assert seen and "evidence_error" in seen[0]
    assert "dialog_png_base64" not in seen[0]


def test_evidence_is_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    """限流，否则一轮几十张 —— 同 `record_unrecognised_screen` 的先例。"""
    frame = Image.new("RGB", (1920, 917), (17, 23, 34))
    seen = _record(monkeypatch, "UF HK", frame=frame)
    _loop(frame)._record_recycle_dialog_evidence("UF HK")

    assert len(seen) == 1, "限流没生效，第二张也存下来了"


# -- 接线 ------------------------------------------------------------------------


def test_evidence_is_taken_before_the_screen_is_reset() -> None:
    """⚠️⚠️ **源码级断言：取证必须排在 `_reset_to_known_screen()` 之前。**

    写反了**不会报错、跑流程也看不出来** —— 日志里照样有一张图，
    只不过那是复位之后的画面，跟出错时那一帧毫无关系。
    这种「存了但存错了」比不存更坏：它看起来像证据。
    """
    source = inspect.getsource(BotLoop._recycle_once)
    evidence = source.index("self._record_recycle_dialog_evidence(")
    reset = source.index("self._reset_to_known_screen()")

    assert evidence < reset, "取证排在了复位之后 —— 存下来的是复位后的画面，证明不了任何事"
