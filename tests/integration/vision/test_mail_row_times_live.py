"""用真实信箱截图守住一条判据的**前提**：时间那一格比主题稳一个量级。

事故（2026-08-18）：`_scroll_mail_list_to_top` 判「拖不动了」比的是主题 + 时间
拼起来的行身份，而主题那一格读不稳——面板是半透明的，
背后那一页的字（`-TOTAL CREWS`、`-17003`、`personnel`）透上来落进同一块 ROI，
同一封邮件两次读成 `'大 Sw GEF攻击报告 bad'` 与 `'EN SEFATing bad Za once'`。
于是「还是那几封」**永远不成立**，每一趟都走满 40 次上限（近四分钟），
而用户当场核对过：进邮箱本来就在顶部。生产库里那一句当天出现了 17 次。

判据因此改挂在**时间列**上（`mail_times_settled`）。这个文件量的就是那个前提，
在 `var/logs` 的信箱实拍上：

- 形如 `DD/MM/YYYY HH:MM:SS` 的时间，绝大多数行读得出来；
- 主题一字不差的行，**一行都没有**。

真图不进仓库（本仓公开，截图能反推坐标与账号），所以缺图时整个文件跳过——
与 `tests/integration/vision/` 下其余 `*_live.py` 同一条规矩。
"""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

from evo_helper.tools.pirate_loop import mail_row_from_text

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

LOGS = Path("var/logs")
TESSERACT = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

#: 手挑的信箱**列表页**实拍。`var/logs` 下同名前缀的大多数是详情页或别的面板，
#: 混进来只会把统计稀释掉；这几张是确认过的列表页（含到顶的与滚到半路的）。
SCREENS = (
    "rep-8-reports.png",
    # 同一秒真有两封邮件的那一屏（`13:07:42` 上 `远征舰队返回` 与 `远征报告`）。
    # `MailRow.identity` 只认时间那一格，靠的就是「同秒最多是一对」这个前提。
    "rep-7-mail.png",
    "dump-mail-detail-unrendered-053043.png",
    "dump-mail-detail-unrendered-203351.png",
    "dump-mail-detail-unrendered-113633.png",
    "dump-mail-detail-unrendered-234504.png",
)

#: 主题读对了长什么样。命中一个就算「这一行的主题一字不差」。
CLEAN_SUBJECTS = frozenset({"侦察报告", "攻击报告", "防御报告", "海盗攻击报告", "探索报告"})

_paths = [LOGS / name for name in SCREENS]

pytestmark = pytest.mark.skipif(
    not (all(path.is_file() for path in _paths) and Path(TESSERACT).is_file()),
    reason="信箱实拍或 Tesseract 不在",
)


def _rows(path: Path) -> list:
    from evo_helper.vision.optional.report_screens import ImageReportScreens
    from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport

    image = crop_to_viewport(Image.open(path).convert("RGB"))
    screens = ImageReportScreens(
        image, layout_for_viewport(image.width, image.height), tesseract_cmd=TESSERACT
    )
    return [mail_row_from_text(index, text) for index, text in enumerate(screens.mail_rows())]


def test_the_time_column_is_readable_where_the_subject_is_not() -> None:
    """时间读得出的行数**远多于**主题读得对的行数。判据挂在时间上就是因为这个。

    阈值刻意留松（时间 ≥ 80%，主题 == 0）：这里要守的是两者之间那个数量级的
    差距，不是某一版 Tesseract 的具体成绩。真要哪天主题也读稳了，也得先让这条
    红一次、有人看过，才轮到改判据。
    """
    rows = [row for path in _paths for row in _rows(path)]
    timed = [row for row in rows if row.raw_time_text]
    clean = [row for row in rows if row.subject in CLEAN_SUBJECTS]

    assert rows, "一行都没读出来，先看 ROI 是不是错了"
    assert len(timed) >= 0.8 * len(rows), f"只有 {len(timed)}/{len(rows)} 行读出时间"
    assert not clean, f"主题居然读对了 {len(clean)} 行：{[row.subject for row in clean]}"


def test_mails_that_share_a_second_come_in_pairs_not_screenfuls() -> None:
    """同一秒的邮件**最多成对**，不会是一整屏。这是「身份只认时间」的前提。

    `MailRow.identity` 拿时间那一格当跨屏去重的身份（理由整段在那里），代价是
    同秒两封在观测上不可分：那一对正好被屏幕下边缘切开时，后一封这一趟会被
    当成重复跳过。**这个代价的大小完全取决于同秒能挤几封**——一对就是偶尔漏一封
    （下一趟还能读到），一屏就是整页塌掉。

    实拍上确实有一对（`rep-7-mail.png`：`08/08/2026 13:07:42` 同时是
    `远征舰队返回` 和 `远征报告`，舰队落地那一瞬间同时产出通知和报告），
    但没有任何一屏出现三行同秒。阈值就守在这里：真哪天挤出三封，得先让这条红
    一次、有人看过，才轮到讨论要不要换别的身份。
    """
    worst: dict[str, int] = {}
    for path in _paths:
        counts = Counter(row.raw_time_text for row in _rows(path) if row.raw_time_text)
        worst[path.name] = max(counts.values(), default=0)

    assert worst, "一屏都没读出来，先看 ROI 是不是错了"
    crowded = {name: most for name, most in worst.items() if most > 2}
    assert not crowded, f"有屏出现三行以上同秒：{crowded}"


def test_the_same_screen_read_twice_agrees_on_the_times() -> None:
    """同一屏读两遍，时间列逐位相同——`mail_times_settled` 的「没动」就靠这个。

    这条只证明**判据不会自相矛盾**（同样的像素给同样的答案）；实机上还叠着
    重新截屏的抖动，那一层由「严格多数」那道余量兜着，见 `mail_times_settled`。
    """
    path = _paths[0]

    first = [row.raw_time_text for row in _rows(path)]
    second = [row.raw_time_text for row in _rows(path)]

    assert first == second
    assert any(first), "这一屏一个时间都没读出来，样本选错了"
