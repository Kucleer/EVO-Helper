"""用实拍守住「我在自己星球地表」那个正面凭据。

## 事故（2026-08-12）

`MAIL_BADGE_ROI` 是 `PirateLoop._on_planet_surface()` 唯一的正面凭据。它标定时
未读数是 **70**（两位），后来涨到 160 / 196 / 332（三位）——数字**居中于
x≈1165**，每多一位就同时往左往右各长 4.5px，于是三位数顶出了那个框。

后果不是「读错」，是**整块读成空**：`_enter_mailbox` 报「切不到自己星球地表，
读不了信箱；安全停止」，而现场图上游戏好端端停在地表、信箱图标就在右上角。
那一夜 BOT 在 15 发 bot 攻击的 6 小时死线内只跑起过三轮，两轮倒在这里，
21 份战报全部过期判缺失。

    23:52:12  已存现场 var\\logs\\dump-planet-surface-unreachable-235211.png
              ROI(1145, 55, 1200, 92) 读到 ''
    00:30:46  同上（dump-planet-surface-unreachable-003046.png）

## 这个文件钉的是什么

**判据的两面**，两面都不能只靠单元测试——单元测试喂的是读数清单，
而「这块像素读不读得出数字」只有真实像素回答得了（先例见
`test_briefing_flight_live.py`：那个 ROI 从落地起就没读出过东西，单元测试全绿）。

- **正面**：明明停在地表的现场图，必须读得出数字。
- **负面**：浮层压着 / 恒星系视图（那个位置是绿色的资源牌）的现场图，
  必须读成空——否则助手会在认不出的画面上照地表的坐标点下去。

截图在 `var/` 下，不进 Git，缺图时整个文件跳过。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evo_helper.tools.pirate_loop import (
    MAIL_BADGE_ROI,
    MAIL_BADGE_THRESHOLD,
    MAIL_BADGE_UPSCALES,
)

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

LOGS = Path("var/logs")

#: 「游戏就停在地表，信箱角标看得见」的现场图 → 画面上那个未读数。
#:
#: 前两张是事故当天存下来的，`ROI 读到 ''` 那两行日志说的就是它们。
#: 后两张补的是**位数**与**版面微移**：332 同样是三位，而 65 那张整个部件右移了
#: 约 4px（信封白块 1120–1147 而不是 1115–1144），数字中心却还在 1165 上。
ON_SURFACE = {
    "dump-planet-surface-unreachable-235211.png": "160",
    "dump-planet-surface-unreachable-003046.png": "196",
    "dump-planet-surface-unreachable-053427.png": "332",
    "dump-planet-surface-unreachable-212858.png": "65",
}

#: 角标位置上**不是**信箱角标的现场图。恒星系视图与各种浮层在这一格是那块绿色的
#: 资源牌（一个圈起来的 `$`），读出任何数字都意味着「浮层被判成了地表」。
NOT_ON_SURFACE = (
    "dump-mail-detail-unrendered-002300.png",
    "dump-briefing-unrecognised-071619.png",
    "rep-3-maillist.png",
    # 这一张是**被模态压暗的**地表：角标还在，但整屏灰下去了。判成「不在地表」是
    # 对的（有东西盖着就不许照地表的坐标点），而它落到这一侧全靠二值化。
    "dump-bot-coord-mismatch-235334.png",
)

#: 只在这几条里用到、不属于上面两组的实拍。
EXTRA_SHOTS = ("rank-closed.png",)

pytestmark = pytest.mark.skipif(
    not all((LOGS / name).exists() for name in (*ON_SURFACE, *NOT_ON_SURFACE, *EXTRA_SHOTS)),
    reason=f"缺实拍截图（{LOGS}/dump-planet-surface-unreachable-*.png 等）",
)


@pytest.fixture(scope="module")
def ocr():  # type: ignore[no-untyped-def]
    from evo_helper.tools.scan_coordinates import make_ocr

    return make_ocr()


def _badge(ocr, name: str, *, roi=MAIL_BADGE_ROI, threshold=MAIL_BADGE_THRESHOLD) -> str:  # type: ignore[no-untyped-def]
    """照 `PirateLoop.mail_badge_text()` 那条路读一遍：逐倍数试到读出纯数字为止。"""
    crop = Image.open(LOGS / name).crop(roi)
    for upscale in MAIL_BADGE_UPSCALES:
        text = ocr(crop, digits=True, upscale=upscale, threshold=threshold).strip()
        if text.isdigit():
            return text
    return ""


@pytest.mark.parametrize("name", sorted(ON_SURFACE))
def test_the_badge_is_readable_on_every_surface_shot(ocr, name: str) -> None:  # type: ignore[no-untyped-def]
    """事故的直接守卫：这四张上必须**读得出数字**。

    ⚠️ 断言的是「非空」而不是「等于 160」，因为判据本身就只看非空——把面板描边
    一起读成 `4160` 是无害的，读成空才是致命的。真值另有一条钉着（下一条），
    分开是为了让这条在 OCR 抖动出一个多余数字时**不会**变红：它守的是那次事故，
    而那次事故是空。
    """
    assert _badge(ocr, name) != ""


@pytest.mark.parametrize("name", sorted(ON_SURFACE))
def test_the_badge_reads_the_number_on_the_screen(ocr, name: str) -> None:  # type: ignore[no-untyped-def]
    """读出来的确实是画面上那个数（而不是碰巧读到别处的噪声）。

    这一条与上一条的区别是**它允许日后被放宽**：框只框得住后几位时，读数会
    变成 `2345` 这样的后缀，那时该改的是这一条，上面那条「非空」一个字都不能动。

    不过按用户口径（2026-08-13）「邮箱不需要考虑 4 位数情况」，这一天多半不会来
    ——写在这里只是说明两条的分工，不是在为它做准备。
    """
    assert _badge(ocr, name) == ON_SURFACE[name]


@pytest.mark.parametrize("name", sorted(NOT_ON_SURFACE))
def test_the_badge_stays_empty_when_something_covers_it(ocr, name: str) -> None:  # type: ignore[no-untyped-def]
    """负面：不在地表时这一块必须读成空。

    这条是「把框放宽」的刹车。实测把左界推到 1140（吃进信封白块）之后，
    这几张会从暗面板纹理里读出 `'2'`、`'3'`、`'7'`——于是浮层被判成地表，
    助手会在浮层上照地表的坐标点一下（`MAIL_BUTTON`）。
    """
    assert _badge(ocr, name) == ""


def test_the_old_roi_is_what_lost_the_reports(ocr) -> None:  # type: ignore[no-untyped-def]
    """把事故成因本身钉住：旧框 + 旧读法在那两张上读到的就是空。

    没有这条，日后有人「顺手收窄一点」会一路绿灯回到 2026-08-12。
    """
    old_roi = (1145, 55, 1200, 92)
    for name in ("dump-planet-surface-unreachable-235211.png",):
        crop = Image.open(LOGS / name).crop(old_roi)
        assert ocr(crop, digits=True, upscale=3).strip() == ""


def test_the_left_edge_must_stay_clear_of_the_envelope_icon(ocr) -> None:  # type: ignore[no-untyped-def]
    """左界不能再往左：信封那个白块二值化之后是一大团纯白，psm 7 会被它压垮。

    这条是 `MAIL_BADGE_ROI` 注释里「左界 1148 = 信封白块右缘再往右 1px」的凭据，
    也是**「框放宽一点总没坏处」这个直觉的反例**：这一侧放宽会直接把正面读成空，
    而正面读成空就是 2026-08-12 那 21 份战报的死因。
    """
    left, top, right, bottom = MAIL_BADGE_ROI
    swallowed = (left - 5, top, right, bottom)

    assert _badge(ocr, "rank-closed.png") != ""
    assert _badge(ocr, "rank-closed.png", roi=swallowed) == ""


def test_the_vertical_edges_must_not_be_widened_either(ocr) -> None:  # type: ignore[no-untyped-def]
    """上下也不能放：各推 6px 就够把这张读成空。

    单独一条，是因为「读不出来就把框放大点」是最自然的下一步反应，而这一格
    **四条边都在悬崖边上**——数字只有 16px 高、24px 宽，多框进去的每一行像素
    都是面板描边或信封的辉光。
    """
    left, top, right, bottom = MAIL_BADGE_ROI
    loosened = (left, top - 6, right, bottom + 6)

    assert _badge(ocr, "dump-planet-surface-unreachable-212858.png") == "65"
    assert _badge(ocr, "dump-planet-surface-unreachable-212858.png", roi=loosened) == ""


def test_a_partial_read_is_still_a_valid_credential(ocr) -> None:  # type: ignore[no-untyped-def]
    """读到半截也算数——这条是「四位、五位怎么办」那个问题的另一半答案。

    `rank-closed.png` 画面上是 `118`，这套配方读出来是 `'8'`。判据只问「读不读得
    出数字」，所以半截照样成立；真到了五位数被左界切一刀，读出来是「糊掉的首位 +
    后几位」也一样。

    ⚠️ 反过来说：**谁要是哪天拿这个数去做判断，先回来看这条。**
    """
    assert _badge(ocr, "rank-closed.png") == "8", "画面上是 118；读到半截是已知且可接受的"


def test_the_binarisation_is_what_keeps_the_dimmed_screens_out(ocr) -> None:  # type: ignore[no-untyped-def]
    """不二值化就守不住负面——把 `MAIL_BADGE_THRESHOLD` 存在的理由钉住。

    盖住画面的模态会把整屏压暗，角标连同信封一起变成暗灰。不二值化时那一屏照样
    读得出数字（这张读作 `166`），于是「有模态压着」被判成「在地表」，
    助手接着就会照地表的坐标往那个模态上点。
    """
    assert _badge(ocr, "dump-bot-coord-mismatch-235334.png") == ""
    assert _badge(ocr, "dump-bot-coord-mismatch-235334.png", threshold=None) == "166"
