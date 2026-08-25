"""导航栏回读对不上时，把三个值框按**原分辨率**存进日志。

## 为什么

`agreed_value` 只看得见「配方读出了什么」，看不见「屏上到底有几位数字」。剩下
读不出的 336 个格子全卡在这一点，而且**光看读数救不回来**：

    真值 261 ← ['261','26','26','6','61']       2 票短值 + 1 票长值 → 长的对
    真值 391 ← ['3','3931','391','331','391']   2 票短值 + 1 票长值 → 短的对

投票形态一模一样。要分开只能去数屏上有几位数字，**而那需要像素**。

原先只存整帧缩到 480 宽的缩略图，135×33 的值框在上面是 **34×8**、数字剩 3.5px，
实测就是两团糊斑。⇒ 两次标定翻车（2026-08-18、2026-08-25）的共同根因，是手里
从来没有会出错的那些字形的真像素。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.game.system_navigator import NAV_BOX_LABELS, NAV_VALUE_ROIS

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")


def _frame() -> Any:
    """一张 1920×917 的假整帧。内容无所谓——这些用例量的是几何与预算。"""
    return Image.new("RGB", (1920, 917), (17, 23, 34))


# -- 裁片本身 --------------------------------------------------------------------


def test_the_crops_come_out_at_full_resolution() -> None:
    """⚠️⚠️ **一个像素都不许缩。**

    这就是整件事的要点：值框是 135×33，缩略图那条路把它变成 34×8，数字从 14px
    高剩 3.5px —— 数不出是三位数。存了却数不出，等于没存。
    """
    from evo_helper.game.system_navigator import value_box_crops

    for (_label, crop), roi in zip(value_box_crops(_frame()), NAV_VALUE_ROIS, strict=True):
        assert crop.size == (roi[2] - roi[0], roi[3] - roi[1])
        assert crop.size == (135, 33)


def test_the_crops_are_labelled_in_coordinate_order() -> None:
    """三块的名字要和坐标的三段对得上，否则语料的真值会整体错位。

    ⚠️ 错位不会报错，只会让重挑配方时拿 `galaxy` 的图去配 `position` 的真值 ——
    一个静默地把标定引向错误结论的 bug。
    """
    from evo_helper.game.system_navigator import value_box_crops

    assert [label for label, _crop in value_box_crops(_frame())] == list(NAV_BOX_LABELS)
    assert NAV_BOX_LABELS == ("galaxy", "system", "position")


def test_the_crops_are_not_pre_processed() -> None:
    """⚠️ 不转灰、不二值化。

    配方本身就在调放大倍数和二值化阈值。先处理一道等于把标定的自由度提前焊死 ——
    存下来的会是「按今天这套参数处理过的样子」，而不是屏上的样子，
    于是这份语料对「换一套阈值会怎样」一个字都答不上来。
    """
    from evo_helper.game.system_navigator import value_box_crops

    frame = _frame()
    _label, crop = value_box_crops(frame)[0]

    assert crop.mode == frame.mode


# -- 存进日志的预算 ---------------------------------------------------------------


class _Driver:
    def __init__(self) -> None:
        self.captures = 0
        self.frame: Any = None

    def capture(self) -> Any:
        self.captures += 1
        return self.frame if self.frame is not None else _frame()


class _Recorder:
    """替掉 `record_system_log`，把 payload 收下来。"""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, _level: str, _source: str, _message: str, *, payload: Any) -> None:
        self.payloads.append(payload)


@pytest.fixture
def looper(monkeypatch):  # type: ignore[no-untyped-def]
    """一个只装了这条证据链所需零件的 `PirateLoop`。"""
    from evo_helper.tools import pirate_loop as module

    recorder = _Recorder()
    monkeypatch.setattr(module, "record_system_log", recorder)
    looper = module.PirateLoop.__new__(module.PirateLoop)
    looper._driver = _Driver()  # type: ignore[attr-defined]
    looper._nav_readback_dumps = 0  # type: ignore[attr-defined]
    looper._nav_value_crop_shapes = set()  # type: ignore[attr-defined]
    return looper, recorder, module


def _reading(module: Any, position: tuple[str, ...]) -> Any:
    """⚠️ **`frame` 带的是「读出这些字的那一帧」**，不是随手截的另一张。

    见 `NavBarReading.frame`：第一版落证据时自己又 `capture()` 了一次，存下来的
    像素与记下来的读数对不上。夹具照着真实契约给，才测得出这件事。
    """
    return module.NavBarReading(
        values=("4", "277", ""),
        reads=(("4",) * 5, ("277",) * 5, position),
        frame=_frame(),
    )


def test_the_crops_come_from_the_frame_that_was_read(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️⚠️ **裁片取自 `reading.frame`，不许另截一张。**

    构造成两者可分辨：`reading.frame` 是纯黑，而驱动的 `capture()` 交的是纯白。
    存下的裁片必须是黑的——若实现回到「自己再截一张」，它会是白的，当场红。

    这一条钉的是 2026-08-25 撞见的真事：同一条告警日志记着
    `['261','26','26','6','61']`，拿存下的裁片重跑五套配方却给出
    `['261','261','26','6','261']` —— 两次截屏之间画面动过。
    这份语料**唯一的用途**就是拿真像素去标定配方，错配的像素会把标定引向一个
    根本不存在的问题。
    """
    import base64
    import io

    from evo_helper.domain.models import Coordinate

    obj, recorder, module = looper
    obj._driver.frame = Image.new("RGB", (1920, 917), (255, 255, 255))
    black = module.NavBarReading(
        values=("4", "277", ""),
        reads=(("4",) * 5, ("277",) * 5, ("6", "1", "15", "15", "15")),
        frame=Image.new("RGB", (1920, 917), (0, 0, 0)),
    )

    obj._record_navigation_bar_mismatch(Coordinate(4, 277, 15), black)

    encoded = recorder.payloads[0]["value_box_png_base64"]["galaxy"]
    saved = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert saved.getpixel((0, 0)) == (0, 0, 0), "裁片来自另一帧，不是读出那些字的那一帧"


def test_no_frame_means_no_crops_but_the_text_still_lands(looper) -> None:  # type: ignore[no-untyped-def]
    """轻量驱动截不了图时 `frame` 是 None —— 不落裁片，但那条告警照记。"""
    from evo_helper.domain.models import Coordinate

    obj, recorder, module = looper
    reading = module.NavBarReading(
        values=("4", "277", ""), reads=(("4",) * 5, ("277",) * 5, ("15",) * 5)
    )

    obj._record_navigation_bar_mismatch(Coordinate(4, 277, 15), reading)

    assert recorder.payloads[0]["expected"] == "4:277:15"
    assert "value_box_png_base64" not in recorder.payloads[0]


def test_the_value_boxes_ride_along_with_the_mismatch_record(looper) -> None:  # type: ignore[no-untyped-def]
    """三块裁片跟着那条告警一起落库，按框名分开放。"""
    from evo_helper.domain.models import Coordinate

    obj, recorder, module = looper

    obj._record_navigation_bar_mismatch(
        Coordinate(4, 277, 15), _reading(module, ("6", "1", "15", "15", "15"))
    )

    crops = recorder.payloads[0]["value_box_png_base64"]
    assert sorted(crops) == sorted(NAV_BOX_LABELS)
    assert all(value for value in crops.values())


def test_a_repeat_of_the_same_shape_does_not_spend_another_slot(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️⚠️ **同一种读数形态只存一次。**

    生产 2026-08-19 → 08-25 的 430 次告警去重之后只有 **27 种**形态，其中 134 次
    是同一颗星球的同一格。按次数封顶会把名额全花在重复上，攒一个月也只有一两种
    字形——而这些裁片存在的唯一意义就是**攒字形**。
    """
    from evo_helper.domain.models import Coordinate

    obj, recorder, module = looper
    same = _reading(module, ("6", "1", "15", "15", "15"))

    obj._record_navigation_bar_mismatch(Coordinate(4, 277, 15), same)
    obj._record_navigation_bar_mismatch(Coordinate(4, 277, 15), same)

    assert "value_box_png_base64" in recorder.payloads[0]
    assert "value_box_png_base64" not in recorder.payloads[1], "同一形态存了两次"


def test_a_new_shape_still_gets_its_slot(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 反过来：**没见过的形态照存不误。**

    这一条和上一条是一对。少了它，去重可能被写成「一轮只存一次」——那就退回
    「按次数封顶」，正是要避免的东西。
    """
    from evo_helper.domain.models import Coordinate

    obj, recorder, module = looper

    obj._record_navigation_bar_mismatch(
        Coordinate(4, 277, 15), _reading(module, ("6", "1", "15", "15", "15"))
    )
    obj._record_navigation_bar_mismatch(
        Coordinate(4, 277, 15), _reading(module, ("", "7", "7", "7", "7"))
    )

    assert all("value_box_png_base64" in payload for payload in recorder.payloads)


def test_the_budget_stops_at_the_cap(looper) -> None:  # type: ignore[no-untyped-def]
    """名额用完就不再存 —— 一条日志几 KB，跑失控了会把日志表灌满。"""
    from evo_helper.domain.models import Coordinate

    obj, recorder, module = looper
    obj.MAX_NAV_VALUE_CROPS = 2

    for index in range(4):
        obj._record_navigation_bar_mismatch(
            Coordinate(4, 277, 15), _reading(module, (str(index), "1", "15", "15", "15"))
        )

    kept = [p for p in recorder.payloads if "value_box_png_base64" in p]
    assert len(kept) == 2


def test_the_whole_frame_thumbnail_keeps_its_own_separate_budget(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 整帧缩略图和值框裁片**各花各的名额**。

    两者回答的问题不同：缩略图说「当时屏上大致是什么」（导航栏在不在、有没有浮层
    盖住），裁片说「这几个字长什么样」。合用一个预算的话，攒字形会把排障用的那两张
    整帧图挤掉——而 2026-08-24 那次太空舱盖住导航条，正是靠整帧图定的位。
    """
    from evo_helper.domain.models import Coordinate

    obj, recorder, module = looper
    obj.MAX_NAV_READBACK_FRAMES = 1

    for index in range(3):
        obj._record_navigation_bar_mismatch(
            Coordinate(4, 277, 15), _reading(module, (str(index), "1", "15", "15", "15"))
        )

    frames = [p for p in recorder.payloads if "thumbnail_png_base64" in p]
    crops = [p for p in recorder.payloads if "value_box_png_base64" in p]
    assert len(frames) == 1
    assert len(crops) == 3


def test_a_driver_that_cannot_screenshot_still_records_the_text(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 驱动截不了整帧时，**文字照记**，而且裁片照落。

    两者的来源不同：裁片来自 `reading.frame`（读那些字时就已经在手上了），
    整帧缩略图才需要现截一张。所以「截不了图」只该少掉缩略图那一样。

    证据是锦上添花，不许把这条告警本身弄没了——「这一轮一发都不派」的原因
    首先要有一句话说得清。
    """
    from evo_helper.domain.models import Coordinate

    obj, recorder, module = looper
    obj._driver = object()

    obj._record_navigation_bar_mismatch(
        Coordinate(4, 277, 15), _reading(module, ("6", "1", "15", "15", "15"))
    )

    assert recorder.payloads[0]["expected"] == "4:277:15"
    assert "thumbnail_png_base64" not in recorder.payloads[0]
    assert "value_box_png_base64" in recorder.payloads[0]
