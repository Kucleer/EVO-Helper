"""进信箱前必须先关浮层，切不过去要留下现场。

`_on_planet_surface()` 的**正面凭据是右上角信箱旁边的未读数**，而它自己的注释就
写着「浮层会盖住它」。可 `_goto_planet_surface()` 并不关浮层——它只会反复点视图
菜单，而那个坐标此刻正压在浮层底下。

这一步偏偏紧跟在 `_wait_for_reports()` 的 45 秒等待之后，正是舰队返航之类的通知
最容易冒出来的时刻。实机三次都倒在这里：

    02:10  切不到自己星球地表，读不了信箱；安全停止
    03:35  同上
    03:46  同上（同一轮里 2:137 的报告读成功过，换到 2:136 才倒）

每次都已经先派出 4 发侦察，报告读不到那 4 发就白飞；而 `RuntimeError` 计入连续
失败，三次就把整条链路自动停用。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.tools.pirate_loop import LoopOptions, PirateLoop

WANTED = (Coordinate(2, 136, 1),)


def _loop(*, reachable: bool) -> tuple[Any, list[str]]:
    events: list[str] = []
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(systems=(), scout=True, attack=True)  # type: ignore[attr-defined]
    loop._started_at = datetime(2026, 8, 11, tzinfo=UTC)  # type: ignore[attr-defined]
    loop._reset_to_known_screen = lambda: events.append("关浮层")  # type: ignore[assignment, method-assign]
    loop._goto_planet_surface = lambda: (  # type: ignore[assignment, method-assign]
        events.append("切地表"),
        reachable,
    )[1]
    loop._dump_frame = lambda name, roi=None: events.append(f"存图:{name}")  # type: ignore[assignment, method-assign]
    # 逐套配方把未读数打进日志，是纯诊断（要真截图，所以这里打桩）。
    loop._say_mail_badge_reads = lambda: events.append("打印角标读数")  # type: ignore[assignment, method-assign]
    loop._open_mail = lambda: events.append("开信箱")  # type: ignore[assignment, method-assign]
    # 拖回顶部另有专文（`test_mailbox_scroll_to_top.py`）。
    loop._scroll_mail_list_to_top = lambda: events.append("拖回顶部")  # type: ignore[assignment, method-assign]
    return loop, events


def test_overlays_are_closed_before_the_surface_check() -> None:
    """本文件的重点：关浮层必须排在切地表**之前**。

    顺序反了等于没修——判据读的就是被浮层盖住的那个未读数。
    """
    loop, events = _loop(reachable=True)

    # 开信箱之后的读报告流程不在这条测试的范围内，用一个哨兵异常截断。
    loop._open_mail = lambda: (_ for _ in ()).throw(RuntimeError("到此为止"))  # type: ignore[assignment, method-assign]
    with pytest.raises(RuntimeError, match="到此为止"):
        loop.collect_scout_reports(WANTED)

    assert events == ["关浮层", "切地表"]


def test_an_unreachable_surface_leaves_a_frame_behind() -> None:
    """切不过去要留下两样：一帧现场图，和**逐套配方的原始读数**。

    只有图不够。2026-08-12 那两条日志留的是「ROI(1145, 55, 1200, 92) 读到 ''」
    ——一个数、一套配方，事后要把那张图重新喂一遍 OCR 才看得出是哪一套读空的、
    读到的又是什么。判据现在是「三套里有一套读出纯数字」，日志也得三套都说。
    """
    loop, events = _loop(reachable=False)

    with pytest.raises(RuntimeError, match="切不到自己星球地表"):
        loop.collect_scout_reports(WANTED)

    assert events == ["关浮层", "切地表", "存图:planet-surface-unreachable", "打印角标读数"]


# -- 未读数怎么读的 ----------------------------------------------------------


def _badge_loop(reads: dict[int, str]) -> Any:
    """一个「每个放大倍数读到什么」都由剧本给定的 loop。"""
    loop = PirateLoop.__new__(PirateLoop)
    loop._read = lambda roi, **kwargs: reads.get(kwargs.get("upscale"), "")  # type: ignore[assignment, method-assign]
    return loop


def test_the_badge_falls_back_to_the_next_upscale() -> None:
    """第一套配方读不出来就换下一套——**这是「多套配方」那条路径本身**。

    现有 22 张实拍上 3× 一套就够，所以像素那侧证明不了这条路会被走到；
    而它存在的理由恰恰是「今天够用」这件事靠不住：未读数会变、版面会微移，
    上一次「一套恰好够用」的结论就是 2026-08-12 那次事故的起点。
    """
    from evo_helper.tools.pirate_loop import MAIL_BADGE_UPSCALES

    first, second = MAIL_BADGE_UPSCALES[0], MAIL_BADGE_UPSCALES[1]

    assert _badge_loop({first: "", second: "332"}).mail_badge_text() == "332"


def test_a_read_that_is_not_all_digits_does_not_count() -> None:
    """读到 `':'`、`'7 :'` 这类不算数——数字白名单里有冒号（给坐标行 `2:137:18` 用的）。

    只判非空的话，别的画面上的纹理噪声就能把浮层判成地表，接着助手会照地表的
    坐标往浮层上点一下。收成 lanczos + 二值化之后现有实拍里已经不出现这种读数了，
    所以这条只能在这一层守——但它守的是**判据**，不是某一张图。
    """
    from evo_helper.tools.pirate_loop import MAIL_BADGE_UPSCALES

    junk = dict.fromkeys(MAIL_BADGE_UPSCALES, "7 :")

    assert _badge_loop(junk).mail_badge_text() == ""
    assert _badge_loop({**junk, MAIL_BADGE_UPSCALES[-1]: "70"}).mail_badge_text() == "70"
