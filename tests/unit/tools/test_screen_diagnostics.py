"""认不出画面时，得留下**能定位问题的证据**；以及缺中文语言包要当场停。

⚠️ 这一批全部来自 2026-08-17 的一次实机事故。另一台机器的 Tesseract 只装了
`eng` / `osd`，画面上每一处中文都读成拉丁噪声：

    导航条「行星 舰队 太空舱 商店 联盟」  →  '72 MB = oKSAtC(itéiaG EA'
    入口页「进入」                        →  ''
    英文那几屏                            →  'member hitting you did I ?'（读得挺像）

`IN_GAME_MARKERS` 全是中文，于是**永远判不出「在游戏里」**：每轮都掉到最后去试
START、判 `unrecognised screen`、关窗重开 Chrome、再认不出，循环一小时，环境故障
计数打到 6/6 上限——而日志里翻来覆去只有一句 `unrecognised screen`，**一个字都没说
它到底看到了什么**。最后是靠人肉在那台机器上手工跑探针把 OCR 读数打出来才定的位。

所以这里钉三件事：缺语言包要响地停、认不出要留证据、证据不许把链路刷爆。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.game.session_keeper import ScreenState, classify_screen
from evo_helper.tools import scan_coordinates

# -- 缺中文语言包：当场停，别带着这个残疾开工 ----------------------------------


def test_a_missing_chinese_language_pack_stops_the_runner(monkeypatch) -> None:
    """只有 `eng` / `osd` 时必须抛，而且错误里要点名 `chi_sim`。

    停下来的理由：带着它开工不会报错，只会**安静地空转一整夜**。
    """
    monkeypatch.setattr(
        scan_coordinates, "installed_ocr_languages", lambda _path: frozenset({"eng", "osd"})
    )

    with pytest.raises(RuntimeError, match="chi_sim"):
        scan_coordinates.require_chinese_ocr()


def test_the_chinese_pack_being_present_is_not_an_error(monkeypatch) -> None:
    monkeypatch.setattr(
        scan_coordinates, "installed_ocr_languages", lambda _path: frozenset({"eng", "chi_sim"})
    )

    scan_coordinates.require_chinese_ocr()


def test_an_unreadable_language_list_does_not_block_the_runner(monkeypatch) -> None:
    """⚠️ 问不出来时**放行**。

    `--list-langs` 的输出格式不是契约，换个 Tesseract 版本就可能变。认不出就
    别拦路——这道闸是为了抓「确定缺了」，不是为了抓「没看清」。
    """
    monkeypatch.setattr(scan_coordinates, "installed_ocr_languages", lambda _path: frozenset())

    scan_coordinates.require_chinese_ocr()


# -- 认不出画面：把证据连图一起写进库 ------------------------------------------


class _FakeImage:
    """够用的假图：只要有 size，和一个会失败的 save。"""

    def __init__(self, size: tuple[int, int] = (1920, 917)) -> None:
        self.size = size
        self.width, self.height = size

    def resize(self, size: tuple[int, int]) -> _FakeImage:
        return _FakeImage(size)

    def convert(self, _mode: str) -> _FakeImage:
        return self

    def save(self, *_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("这张假图存不了")


@pytest.fixture
def recorded(monkeypatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scan_coordinates,
        "record_system_log",
        lambda level, source, message, **kwargs: rows.append(
            {"level": level, "message": message, **kwargs}
        ),
    )
    monkeypatch.setattr(scan_coordinates, "_last_evidence_at", None)
    return rows


def test_the_evidence_names_the_size_and_what_each_roi_read(recorded) -> None:
    """三样缺一不可：尺寸、导航条读数、入口标题读数。

    - 尺寸不对 → 窗口没最大化 / 缩放不对 / 抓错了窗口
    - 中文读成拉丁字母 → 语言包问题（本例）
    - 读成空 → ROI 落偏或被浮层盖住

    三种的善后完全不同，所以三样都得留，只留结论等于没留。
    """
    wrote = scan_coordinates.record_unrecognised_screen(
        _FakeImage(),
        nav_text="72 MB = oKSAtC(itéiaG EA",
        entry_text="",
        now=lambda: 0.0,
    )

    assert wrote
    entry = recorded[0]
    assert entry["level"] == "WARNING"
    assert "(1920, 917)" in entry["message"]
    assert "oKSAtC" in entry["message"]
    assert entry["payload"]["capture_size"] == [1920, 917]
    assert entry["payload"]["nav_text"] == "72 MB = oKSAtC(itéiaG EA"
    assert entry["payload"]["entry_title_text"] == ""


def test_a_thumbnail_that_cannot_be_encoded_still_leaves_the_text_evidence(recorded) -> None:
    """图存不下来也要把文字证据写进去——诊断路径不许因为配图失败而整条丢掉。"""
    scan_coordinates.record_unrecognised_screen(
        _FakeImage(), nav_text="乱码", entry_text="", now=lambda: 0.0
    )

    assert recorded[0]["payload"]["thumbnail_png_base64"] == ""
    assert recorded[0]["payload"]["nav_text"] == "乱码"


def test_the_evidence_is_rate_limited_so_it_cannot_flood_the_table(recorded) -> None:
    """⚠️ **限流不是省空间，是防刷爆。**

    实机那一夜「认不出」持续了一个多小时，日志每 2 秒一条。同样频率往库里写
    缩略图，一小时上千张。这里用一个可控的时钟把间隔钉死。
    """
    clock = iter([0.0, 1.0, scan_coordinates.UNRECOGNISED_EVIDENCE_INTERVAL_S + 1.0])
    image = _FakeImage()

    wrote = [
        scan_coordinates.record_unrecognised_screen(
            image, nav_text="x", entry_text="", now=lambda: next(clock)
        )
        for _ in range(3)
    ]

    assert wrote == [True, False, True]
    assert len(recorded) == 2


# -- 入口页判据：只认它独有的那两个记号 ----------------------------------------


def test_the_entry_page_wins_even_when_start_bleeds_through() -> None:
    """⚠️ 入口页底下**透着一层淡淡的 START**，所以不能先判 START。

    反过来也不行：START 页的背景里印着 `ETERNAL VOID`。两屏在文字上互相污染，
    换判定顺序只是把错判从一边挪到另一边。出路是先判入口页**独有**的记号。
    """
    assert classify_screen("ETERNAL VOID 进入 START") is ScreenState.ENTRY
    assert classify_screen("START 点击任意位置继续") is ScreenState.ENTRY


def test_the_start_page_is_still_recognised_by_its_own_word() -> None:
    """入口页独有的记号都不在时，`START` 照常算 START——别把这道闸修成只认入口页。"""
    assert classify_screen("START") is ScreenState.START
    assert classify_screen("ETERNAL VOID START") is ScreenState.START


def test_eternal_void_alone_is_only_weak_evidence_for_the_entry_page() -> None:
    """两个独有记号都没读到、也没读到 START 时，`ETERNAL VOID` 才拿来兜底。"""
    assert classify_screen("ETERNAL VOID") is ScreenState.ENTRY


def test_a_maintenance_notice_still_outranks_everything() -> None:
    """维护公告是浮层，底下的 START / 「进入」照样读得出来，它必须排在最前。

    后判就会把一台停机的服务器认成「在入口页上」，然后一路点下去。
    """
    assert classify_screen("服务器维护 进入 START") is ScreenState.MAINTENANCE
