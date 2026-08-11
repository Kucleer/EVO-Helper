"""页眉时间读不出来，整份战报就入不了库。

事故（2026-08-11）：bot 探路战报连着五份卡在

    2:320:11 的战报读不出来：report header has no readable time

而那行时间在现场图上清清楚楚（`11/08/2026 01:32:37`，右上角）。成因是
`report_header` 是一块**两行、带中文、按列读**的宽 ROI：主题行读得很干净，
右上角那行时间在同一次读里被糊成 `'wi'`，`REPORT_TIME_RE` 自然搜不到。

同一个坑仓库里已经踩过一次并留了办法——VS 块的坐标也是「宽裁剪里读不准、各自
开一个窄单行 ROI」（见 `versus_block` 的注释）。这里照搬：窄 ROI + psm 7 +
纯数字白名单，五张现场图全部读对。

两处细节各自有测试盯着，因为它们都不是随手选的：

- **放大倍数是 3 起步，不是布局默认的 2**：实测 2× 有三张把日期首位削掉
  （`11/08/…` 读成 `1/08/…`）。
- **窄读会丢掉日期与时刻之间的空格**（`11/08/202601:32:37`），所以要补回去；
  而 `REPORT_TIME_RE` **不许**为此放松——它还要在整段页眉文本里搜时间，
  放松之后会在一长串数字中间凑出一个假时间。
"""

from __future__ import annotations

from evo_helper.vision.optional.report_screens import REPORT_TIME_UPSCALES
from evo_helper.vision.parsers import REPORT_TIME_RE, normalise_report_time


def test_a_squashed_stamp_gets_its_separator_back() -> None:
    """窄 ROI 实际读出来的形状。"""
    assert normalise_report_time("11/08/202601:32:37") == "11/08/2026 01:32:37"


def test_a_normal_stamp_passes_through() -> None:
    assert normalise_report_time("11/08/2026 01:32:37") == "11/08/2026 01:32:37"


def test_a_stamp_with_surrounding_noise_is_still_found() -> None:
    assert normalise_report_time("发件人:5  11/08/202601:36:12 ") == "11/08/2026 01:36:12"


def test_a_clipped_leading_digit_is_refused() -> None:
    """2× 放大时真实出现过的坏读数：日期首位被削掉。

    宁可判失败也不能猜——补一位就是凭空造出一个不存在的日期，而报告时间
    是战报去重和认领派遣的依据。
    """
    assert normalise_report_time("1/08/202601:37:04") is None


def test_garbage_reads_as_nothing() -> None:
    for raw in ("", None, "wi", "主题: 攻击报告", "01:32:37"):
        assert normalise_report_time(raw) is None


def test_the_strict_regex_is_not_loosened() -> None:
    """本文件最重要的一条：宽读那条正则**不许**跟着放松。

    `REPORT_TIME_RE` 要在整段页眉文本里搜时间。允许日期与时刻之间没有空白之后，
    一长串连续数字里就能凑出一个「合法」时间——而那会被当成报告时间写进库。
    补分隔这件事只属于窄 ROI 那一条路。
    """
    assert REPORT_TIME_RE.search("11/08/202601:32:37") is None
    assert REPORT_TIME_RE.search("11/08/2026 01:32:37") is not None


def test_the_narrow_read_does_not_start_at_the_layout_default() -> None:
    """3× 打头。布局默认是 2×，而 2× 实测会削掉日期首位。"""
    assert REPORT_TIME_UPSCALES[0] == 3
    assert 2 not in REPORT_TIME_UPSCALES[:1]
