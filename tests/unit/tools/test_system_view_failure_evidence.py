"""切不回恒星系视图时，把**标签读成了什么**留下来。

## 为什么

2026-08-25 查这条告警时撞了墙：一天 **24 次**「派出之后切不回恒星系视图」，每次都以
关窗重开 Chrome（40–70 秒）收场，其中还有整轮作废的。而日志里只有「切不回」三个字，
**说不出画面上是什么**，于是两种成因分不开：

- 画面**真的**不在恒星系视图（派出后有动画或浮层没散）；
- 画面就在恒星系视图，是**标签 OCR 读不出**。

第二种不是瞎猜：这排标签和值框是同一条导航栏、纵向只隔 30px，而值框那一半在实机上
会大面积读不出（`9` 五套配方全空）。

⚠️ 同一天已经为「只记结论不记证据」付过一次账：导航栏值框那条上线以来 28 次回读
28 次对不上，而 `payload_json` 是 `{}`、一帧都没留。这个文件钉的就是那一类。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.game.system_navigator import NAV_LABEL_ROI

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")


class _Keeper:
    """假的会话看护：重开永远成功，且不做任何真事。"""

    class _Outcome:
        ready = True
        detail = ""
        restarts_left = 2

    def restart_and_reenter(self, _why: str) -> Any:
        return self._Outcome()


class _Navigator:
    """假的导航器：`ensure_system_view` 照真实现那样**把读屏函数调满 4 次**。

    ⚠️ 调满 4 次是这条夹具的要点，不是细节：真实现是「3 次重试 + 最后再读一次」，
    而生产上 24 次失败里 19 次落在派出后 26–29 秒 —— 正好是这 4 次读屏加上等待。
    夹具少调几次，`label_reads` 的长度这条判据就测不出来。
    """

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.invalidated = 0

    def ensure_system_view(self, read: Any, *, attempts: int = 3) -> bool:
        from evo_helper.game.system_navigator import on_system_view

        for _ in range(attempts + 1):
            if on_system_view(read()):
                return True
        return False

    def invalidate(self) -> None:
        self.invalidated += 1


class _Driver:
    """截得了图的驱动。**截不了图**的那一档用 `object()`，见最后一条用例。"""

    def __init__(self, frame: Any = None) -> None:
        self.frame = frame if frame is not None else Image.new("RGB", (1920, 917), (9, 12, 20))

    def capture(self) -> Any:
        return self.frame


class _Recorder:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.messages: list[str] = []

    def __call__(self, _level: str, _source: str, message: str, *, payload: Any) -> None:
        self.messages.append(message)
        self.payloads.append(payload)


@pytest.fixture
def looper(monkeypatch):  # type: ignore[no-untyped-def]
    from evo_helper.tools import pirate_loop as module

    recorder = _Recorder()
    monkeypatch.setattr(module, "record_system_log", recorder)
    monkeypatch.setattr(module, "say", lambda _message: None)

    loop = module.PirateLoop.__new__(module.PirateLoop)
    loop._view_failure_dumps = 0
    loop._current_planet = None
    loop._driver = _Driver()
    loop._keeper = lambda: _Keeper()
    return loop, recorder, module


def _run(loop: Any, module: Any, answers: list[str]) -> None:
    reads = iter(answers)
    loop._navigator = _Navigator(answers)
    loop._nav_labels = lambda: next(reads, "")
    try:
        loop._require_system_view("派出之后切不回恒星系视图")
    except module.SessionUnavailable:
        pass


# -- 读数留痕 --------------------------------------------------------------------


def test_every_retry_reading_is_recorded(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️⚠️ **每一次重试读到了什么，逐条记下来。**

    这就是这条告警从前缺的东西：只说「切不回」，不说读到了什么。有了它，
    「读到空串」（OCR 读不出）和「读到别的界面的字」（画面真的不在恒星系视图）
    在库里一眼分得开 —— 而这两种要用完全不同的方式修。
    """
    loop, recorder, module = looper

    _run(loop, module, ["", "舰队 商店", "", ""])

    payload = next(p for p in recorder.payloads if "label_reads" in p)
    assert payload["label_reads"] == ["", "舰队 商店", "", ""]


def test_the_number_of_readings_is_itself_evidence(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 读了几次本身是判据。

    真实现是「3 次重试 + 最后再读一次」= 4 次读屏。生产 24 次失败里 19 次落在
    派出后 **26–29 秒**，正好是这 4 次读屏加上 1+2、1+4、1+6 的等待 —— 说明
    **三次机会一次都没读到**，不是「偶尔卡一下」。

    少于 4 条就意味着中途成功过，那是另一回事、要另外查。所以这个长度要能从
    日志里直接数出来。
    """
    loop, recorder, module = looper

    _run(loop, module, ["", "", "", ""])

    payload = next(p for p in recorder.payloads if "label_reads" in p)
    assert len(payload["label_reads"]) == 4


def test_what_was_read_and_how_it_was_judged_are_recorded_apart(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️⚠️ **「读到了什么」和「判成了什么」分开记。**

    这两件事在这条链路上完全不同：读到 `银河系`（只认出一个，判据要两个）说明画面
    多半是对的、是 OCR 只读出了一部分；读到空串说明根本没读出东西；读到
    `商店 舰队` 说明画面**真的**在别的界面。三种成因、三种修法，而「切不回」
    这一句把它们抹成同一句话。

    ⚠️ 构造里那个 `银河系` **非空却判 False** —— 这正是两个字段必须并存的理由：
    只看判据全是 False，看不出「差一点」和「什么都没有」的区别。
    """
    loop, recorder, module = looper

    _run(loop, module, ["银河系", "", "行星", "商店 舰队"])

    payload = next(p for p in recorder.payloads if "label_reads" in p)
    assert list(zip(payload["label_reads"], payload["on_system_view"], strict=True)) == [
        ("银河系", False),
        ("", False),
        ("行星", False),
        ("商店 舰队", False),
    ]


def test_nothing_is_recorded_when_the_view_switch_works(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 切回来了就**一个字都不记**。

    这一支每轮都跑，正常时记一条等于给日志表灌水，也会把真正出事的那几条淹掉
    —— 这个仓已经为日志刷屏付过一次账。
    """
    loop, recorder, module = looper

    _run(loop, module, ["银河系 恒星系 行星"])

    assert [m for m in recorder.messages if "标签读数" in m] == []


def test_the_two_stages_are_told_apart(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 重开之前和重开之后要分得开。

    两者的含义天差地别：重开之前失败是常态（就是这条告警本身），**重开之后仍然
    失败**说明连全新的浏览器会话都切不回去 —— 那已经不是「动画没散」能解释的了，
    整轮会当场作废。混在一起记，就看不出后者有多严重。
    """
    loop, recorder, module = looper

    _run(loop, module, [""] * 8)

    stages = [p["stage"] for p in recorder.payloads if "label_reads" in p]
    assert stages == ["重开之前", "重开之后"]


# -- 现场图 ----------------------------------------------------------------------


def test_the_label_row_crop_is_kept_at_full_resolution(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️⚠️ **标签行按原分辨率存**，理由同值框那次。

    整帧缩略图是 480 宽，1920 → 480 是 4×，450×27 的标签行变成 112×7 —— 中文字
    全糊，事后什么都看不出。而这一小块 PNG 之后只有几 KB，比那张缩略图还小。
    """
    import base64
    import io

    loop, recorder, module = looper
    loop._driver = _Driver(Image.new("RGB", (1920, 917), (9, 12, 20)))

    _run(loop, module, ["", "", "", ""])

    payload = next(p for p in recorder.payloads if "label_row_png_base64" in p)
    crop = Image.open(io.BytesIO(base64.b64decode(payload["label_row_png_base64"])))
    assert crop.size == (NAV_LABEL_ROI[2] - NAV_LABEL_ROI[0], NAV_LABEL_ROI[3] - NAV_LABEL_ROI[1])
    assert crop.size == (450, 27)


def test_the_frames_are_capped_but_the_text_is_not(looper) -> None:  # type: ignore[no-untyped-def]
    """图封顶，文字不封顶。

    `label_reads` 才是这条告警的主证据 —— 它便宜、每次都该有；几张几乎一样的
    现场图对定位没有增量，所以只留几张。
    """
    loop, recorder, module = looper
    loop._driver = _Driver(Image.new("RGB", (1920, 917), (9, 12, 20)))
    loop.MAX_VIEW_FAILURE_FRAMES = 1

    for _ in range(3):
        _run(loop, module, [""] * 8)

    recorded = [p for p in recorder.payloads if "label_reads" in p]
    framed = [p for p in recorded if "label_row_png_base64" in p]
    assert len(recorded) == 6
    assert len(framed) == 1


def test_a_driver_that_cannot_screenshot_still_records_the_readings(looper) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 截不了图时，读数照记。

    证据是锦上添花，不许把这条告警本身弄没了 —— 而读数恰恰是这里最要紧的那一半。
    """
    loop, recorder, module = looper
    loop._driver = object()

    _run(loop, module, ["", "", "", ""])

    payload = next(p for p in recorder.payloads if "label_reads" in p)
    assert payload["label_reads"] == ["", "", "", ""]
    assert "label_row_png_base64" not in payload
