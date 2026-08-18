"""把「那个 ✕ 在不在」这条判据放到**全部实拍**上跑一遍。

单元测试喂的是抄下来的点阵（`tests/support/screens.py`），够钉住判据的形状；
但「阈值落在哪两类画面之间」只有真实像素回答得了。上一次没做这件事的代价记在
`pirate_ui.FLIGHT_RECIPES` 的注释里——那个 ROI 从落地起就从来没读出过东西，
单元测试全绿、变异全红，唯独没人拿真实像素验过。

`var/logs/` 不进 Git（那是实机那台机器的目录），所以这一整个文件在没有实拍的
机器上跳过；实机 / 用户本机跑得到。

2026-08-18 在 330 张 client 空间（1920×917）实拍上的结果：

    认出来的 92 张   IoU 0.873 – 1.000，静默环白占比 ≤ 0.012
    其余 238 张      IoU ≤ 0.546，而那唯一的 0.546（整屏泛白）静默环白占比 = 1.000

中间隔着 0.33 的空档。下面钉的就是这个空档——它一旦被填上，说明判据开始糊了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evo_helper.game.overlay import (
    OVERLAY_CLOSE_GLYPH_MIN_IOU,
    OVERLAY_CLOSE_QUIET_MAX_RATIO,
    look_at_close_button,
)

Image = pytest.importorskip("PIL.Image", reason="requires Pillow")

LOGS = Path("var/logs")

#: client 空间的整窗截图就是这个尺寸；别的（viewport 图、裁片）不参与。
CLIENT_SIZE = (1920, 917)

#: 挑出来的几张，各自代表一类。**逐张点名**是为了让「哪一类判错了」一眼看得出来，
#: 而不是只看到一个总数对不上。
NAMED: dict[str, bool] = {
    # 认出来：各种浮层，✕ 逐像素相同
    "rankv/21-panel.png": True,  # 军力榜面板（点阵就是从这张量的）
    "dump-planet-list-unreadable-153847.png": True,  # 行星列表读空那次的现场
    "rankv/72-bots.png": True,
    "rep-3-maillist.png": True,  # 信箱列表
    "dump-mail-list-unrecognised-175927.png": True,  # 浮层还在滑入，整块偏了几像素
    # 认不出：同一个框里的「返回」双箭头（不是 ✕，两者是两个动作）
    "atk-4-dispatched.png": False,
    "scout1-detail.png": False,
    # 认不出：压根没有浮层
    "atk-0-panel.png": False,  # 恒星系视图——(750, 71) 正压在「银河系」输入框上
    "plist-0.png": False,
    "rank-closed.png": False,  # 星球地表——那儿是等级徽章那一格
    "rankv/00-baseline.png": False,  # 整屏泛白（还在加载）；只有静默环挡得住它
}

pytestmark = pytest.mark.skipif(
    not all((LOGS / name).exists() for name in NAMED),
    reason="缺实拍截图（var/logs/rankv/21-panel.png 等）",
)


def _look(name: str):  # type: ignore[no-untyped-def]
    return look_at_close_button(Image.open(LOGS / name))


@pytest.mark.parametrize(("name", "expected"), sorted(NAMED.items()))
def test_each_named_screenshot_is_judged_the_way_it_looks(name: str, expected: bool) -> None:
    look = _look(name)

    assert look.visible is expected, f"{name}: IoU {look.iou:.3f} 静默环 {look.quiet_ratio:.3f}"


def test_the_all_white_screen_is_the_case_that_needs_the_quiet_ring() -> None:
    """⚠️ 这一张是整批实拍里离阈值最近的一个反例。

    整屏泛白时点阵框里「全中」，IoU = 167/306 = 0.546——离阈值 0.60 只剩 0.054，
    而另一侧真的 ✕ 最低是 0.873。单靠 IoU 的话，这道界是靠那 0.054 撑着的；
    静默环把它拉开成 1.000 对 ≤0.012。
    """
    look = _look("rankv/00-baseline.png")

    assert look.iou == pytest.approx(0.546, abs=0.002)
    assert look.quiet_ratio > OVERLAY_CLOSE_QUIET_MAX_RATIO
    assert not look.visible


def test_the_back_arrow_is_nowhere_near_the_threshold() -> None:
    """« 与 ✕ 之间必须留着一大截空档，不能靠阈值的最后一位小数分开。"""
    look = _look("atk-4-dispatched.png")

    assert look.iou < OVERLAY_CLOSE_GLYPH_MIN_IOU / 1.5


def test_the_whole_corpus_falls_into_two_well_separated_camps() -> None:
    """扫一遍 `var/logs` 里所有 client 空间实拍，钉住那道空档。

    这一条是判据真正的凭据：它不是「几张挑出来的图判对了」，而是**整批实拍上
    两类之间隔着 0.3 以上**。哪天版面改了、或者有人把阈值往中间挪，这里先红。
    """
    accepted: list[tuple[float, str]] = []
    rejected: list[tuple[float, str]] = []
    for path in sorted(LOGS.glob("*.png")) + sorted(LOGS.glob("rankv/*.png")):
        with Image.open(path) as frame:
            if frame.size != CLIENT_SIZE:
                continue
            look = look_at_close_button(frame)
        (accepted if look.visible else rejected).append((look.iou, path.name))

    assert len(accepted) >= 80, "实拍里带 ✕ 的那一批不该只剩几张"
    assert len(rejected) >= 80, "没有浮层的那一批同样不该塌掉"
    assert min(iou for iou, _n in accepted) >= 0.85
    assert max(iou for iou, _n in rejected) <= 0.60
