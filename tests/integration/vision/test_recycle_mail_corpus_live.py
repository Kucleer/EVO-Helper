"""读回收报告，跑在 8 封**真实邮件详情页**上。语料不在仓库里，缺图就跳过。

## ⚠️ 为什么非要实拍不可

这条链路的判据是**从像素上打出来的**，不是设计出来的。同一批语料上实测：

| 做法 | 过容量校验 |
|---|---|
| 窄框单读 | 7/8 |
| 宽框单读 | 6/8 |
| 更宽框单读 | 1/8 |
| #325 那套多帧众数 | 6/8（**有一封选出了错的**） |
| **多框多配方 + 容量不变量挑** | **8/8** |

合成图上这些差别全都量不出来：自己渲染的字条没有图标边缘、没有背后那层会动的
漂浮文字，而那两样正是全部错误的来源。所以**这一份没有合成版替身**——
它红了就是真的回归了。

## 语料长什么样

`var/fixtures/vision/recycle-mail/` 下：

- ``recycle-*.png`` —— `tools.scan_coordinates.LiveDriver.capture()` 的原始整窗截图
  （1920×917，含标题栏），2026-09-12 夜只读采集，一封一张。
- ``readings.json`` —— 采集当时的读数留痕。**这里只用它的文件清单与船数**，
  资源真值不从它取：那份留痕本身就有两封是错的（正是本文件要守住的那两封）。

语料**一律不进 git**（本仓公开，`var/` 在 .gitignore 第一条）。补语料要起游戏，
属于当次授权才能做的事。

## 这里断言的是统计量

**故意不记「哪一封是多少」**——那等于把账号的资源收入抄进公开仓库。
判据是「8 封全部过容量闸」加上「最大偏差不超过这个数」，两者都比逐封清单更严：
任何一封读错，合计立刻偏出 3% 的闸（实测错的那两封偏 20% 与 5555%）。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from evo_helper.domain.recycle_mail import NOMINAL_SHIP_CAPACITY
from evo_helper.vision.recycle_mail_screen import read_recycle_mail

CORPUS = Path("var/fixtures/vision/recycle-mail")
TRUTH = CORPUS / "readings.json"

#: 语料封数（2026-09-12 夜采的那一批）。
CORPUS_SIZE = 8

#: 允许的最大容量偏差。实测这一批最大 0.19%，留到 0.5% 是给 OCR 的抖动留余量。
#:
#: ⚠️ **这个数是判据，不许悄悄放大。** 放到 3% 就等于把闸门本身当成了通过线——
#: 而 3% 是「拦得住错数」的门槛，不是「读得对」的门槛。真读对了偏差在 0.2% 量级。
MAX_DEVIATION = Decimal("0.005")

Image = pytest.importorskip("PIL.Image", reason="需要 Pillow 才能解开语料里的 PNG")

pytestmark = pytest.mark.skipif(
    not TRUTH.is_file(),
    reason=f"缺实拍语料（{CORPUS}/），本机备齐了才跑——它不进仓库",
)


def _frames() -> list[str]:
    raw = json.loads(TRUTH.read_text(encoding="utf-8"))
    return [entry["file"] for entry in raw]


def _screens(name: str) -> object:
    from evo_helper.vision.optional.report_screens import ImageReportScreens
    from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport

    tesseract = pytest.importorskip(
        "evo_helper.tools.scan_coordinates", reason="需要 tesseract 才能读语料"
    )
    image = crop_to_viewport(Image.open(CORPUS / name))
    return ImageReportScreens(
        image,
        layout_for_viewport(image.width, image.height),
        tesseract_cmd=str(tesseract.tesseract_path()),
    )


def test_every_mail_in_the_corpus_reads_and_passes_the_capacity_gate() -> None:
    """本文件的重点：**8 封全读通、全过闸**。

    ⚠️ 其中两封是 #325 那套众数读错的（一封晶体的小数点被切掉、一封金属多了
    两个数量级）。它们现在过闸，靠的不是换了个更好的框——**三个框各有各的死法，
    而它们不在同一封信上同时死**，所以是让容量不变量从多框候选里挑。
    """
    frames = _frames()
    assert len(frames) == CORPUS_SIZE, "语料封数变了——先确认是补了图还是丢了图"

    worst = Decimal(0)
    for name in frames:
        reading = read_recycle_mail(_screens(name))  # 读不通会抛，那就是失败
        total = sum((item.value for item in reading.amounts), Decimal(0))
        expected = Decimal(reading.ships) * Decimal(NOMINAL_SHIP_CAPACITY)
        deviation = abs(total - expected) / expected
        worst = max(worst, deviation)

    assert worst <= MAX_DEVIATION, (
        f"有一封的容量偏差到了 {worst * 100:.2f}%，超过了 {MAX_DEVIATION * 100:.2f}%。"
        "这不是「差一点」——读对的时候偏差在 0.2% 量级，偏出来说明某一格读错了。"
    )


def test_the_ship_count_is_read_on_every_mail() -> None:
    """船数是容量不变量**唯一的锚**，一个候选都出不来就整封作废。

    单独一条是因为它的失败形态最隐蔽：船数读空时上面那条会直接抛「读不出」，
    和「资源格读错」混在同一个断言里，事后分不出是哪一半坏了。

    ⚠️ 2026-09-13 夜改成**出候选**（`recycle_ship_candidates`）：详情页正文行数
    会变，整块上下浮动约 52px，而标定框只有 28px 高 —— 单读一次接不住。
    这里只断言「至少出得来一个候选」；挑哪一个是容量不变量的事，上面那条管。
    """
    for name in _frames():
        screens = _screens(name)
        ships = screens.recycle_ship_candidates()  # type: ignore[attr-defined]
        assert ships, f"{name} 的回收船数一个候选都读不出"
        assert all(n > 0 for n in ships), f"{name} 的船数候选里有 0：{ships}"


def test_a_wrong_ship_candidate_cannot_smuggle_a_reading_through() -> None:
    """⚠️⚠️ **多给几个船数候选，不许换来「读错但自洽」。**

    这是出候选这件事唯一真正的风险：某一档偏移正好框住隔壁那一行的数字，
    而那个数同样是整数、同样能进容量判据。挡住它的是「三格 + 船数」必须
    **一起**自洽 —— 一个错船数几乎不可能让三格恰好凑出来。

    这一条把那句话钉成断言：往候选里掺一个明显错的船数，读出来的结果不许变。
    """
    for name in _frames()[:3]:
        screens = _screens(name)
        honest = read_recycle_mail(screens)

        tainted = _screens(name)
        real = tuple(tainted.recycle_ship_candidates())  # type: ignore[attr-defined]
        # 掺两个假的：一个比真值小一位，一个大一位。
        tainted.recycle_ship_candidates = lambda: (  # type: ignore[attr-defined]
            real[0] // 10 + 1,
            real[0] * 10,
            *real,
        )

        assert read_recycle_mail(tainted).ships == honest.ships, (
            f"{name}：掺进假船数之后读出来的变了 —— 容量闸没挡住"
        )


def test_the_coordinates_are_deliberately_not_read() -> None:
    """⚠️ **这一条守的是一个「不做」。**

    正文里那两个坐标看着是现成的，实测却会读错（`[1:55:6]` 读成 `[1:55:5]`）。
    「读出一个像样的错坐标」会把这一趟的实收记到别人头上，比读不出坏得多，
    所以整条链路不读它们、坐标一律取自被认领的那一发派遣。

    哪天有人「顺手把坐标也读进来」，这里就会红。
    """
    reading = read_recycle_mail(_screens(_frames()[0]))

    assert not hasattr(reading, "origin")
    assert not hasattr(reading, "target")
