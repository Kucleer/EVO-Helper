"""「到达之后才发现目标在保护期，舰队原路返航」这封通知认不认得出。

判据必须**同时**匹配三样：方括号里的坐标、「处于保护状态」、「已返航」。

⚠️ 放宽成只匹配「保护」两个字是这条判据上最贵的一种错法：它会把正常战报误判成
「撞保护期」，于是那份真战报的收获、战损、胜负连同它认领的那一发一起消失。
CLAUDE.md 上记着同一形状的教训——黑洞事件只匹配完整损失短语，因为每份报告页脚
都提「黑洞」二字，裸匹配会误判。下面 `test_a_bare_protection_word_is_not_enough`
那一组就是拿来守这一条的。

夹具里的坐标**全是编的**：真实那两个（用户 2026-08-21 的截图）不进公开仓库。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.vision.parsers import (
    ReportKind,
    classify_report_subject,
    find_protection_bounce_targets,
)
from evo_helper.vision.protection_bounce import (
    ProtectionBounceUnreadable,
    read_protection_bounce,
)

#: 真实版面照抄，只把坐标换成编的：**全角括号、全角逗号、全角句号**，
#: 以及括注里那段 `bot_x_y_z's Planet`。
BODY = "[4:321:9]（bot_4_321_9's Planet）处于保护状态，我方舰队已返航。"

HEADER = "发件人: 系统\n主题: 舰队返航\n20/08/2026 14:29:32"


class _Mail:
    def __init__(self, body: str = BODY, header: str = HEADER) -> None:
        self._body = body
        self._header = header

    def report_header(self) -> str:
        return self._header

    def security_message(self) -> str:
        return self._body


def test_reads_the_target_and_the_mail_time() -> None:
    reading = read_protection_bounce(_Mail())

    assert reading.target == Coordinate(4, 321, 9)
    # 游戏内显示的时刻**就是 UTC**（`vision.parsers.GAME_DISPLAY_ZONE`）。
    assert reading.reported_at_utc == datetime(2026, 8, 20, 14, 29, 32, tzinfo=UTC)
    assert reading.raw_time_text == "20/08/2026 14:29:32"


def test_the_full_width_punctuation_of_the_real_mail_is_matched_verbatim() -> None:
    """真实那一句一字不改地喂进去要认得出。

    ⚠️ 这一条守的是「判据被收紧到匹配不上真实版面」那一侧的错法：全角括号、
    括注里的 `'s`、全角逗号与句号，任何一处被写死成半角都会让它转红。
    """
    assert find_protection_bounce_targets(BODY) == [Coordinate(4, 321, 9)]


@pytest.mark.parametrize(
    "text",
    [
        # 只有「保护」两个字——这正是不许放宽到的那一档。
        "主题: 攻击报告 [4:321:9] 我方获得保护 VICTORY",
        # 战报页脚那种提一嘴的写法。
        "[4:321:9] 战报 保护状态说明见帮助",
        # 有「处于保护状态」，但舰队**抵达**了（真打起来了）——不是这一档。
        "[4:321:9]（x）处于保护状态，我方舰队已抵达。",
        # 有「已返航」但没说为什么——普通的舰队返回通知。
        "[4:321:9]（x）我方舰队已返航。",
        # 两句顺序反了：「已返航」在坐标前面。判据要的是坐标在前。
        "我方舰队已返航。[4:321:9] 处于保护状态",
        # 坐标没有方括号——那是战报正文里的写法，不是这封通知的写法。
        "4:321:9（x）处于保护状态，我方舰队已返航。",
    ],
)
def test_a_bare_protection_word_is_not_enough(text: str) -> None:
    assert find_protection_bounce_targets(text) == []


def test_a_second_coordinate_cannot_be_crossed_over() -> None:
    """两句之间不许跨过另一个坐标去凑一条。

    「[A] …（没说保护）… [B] 处于保护状态…已返航」只该读出 B 一个。跨过去的话，
    保护期会被记到 A 头上，而 A 从此被无故排除出候选池。
    """
    text = "[4:321:9] 舰队已抵达。[5:222:3]（x）处于保护状态，我方舰队已返航。"

    assert find_protection_bounce_targets(text) == [Coordinate(5, 222, 3)]


def test_ocr_may_space_the_characters_out() -> None:
    """`chi_sim` 会在字之间塞空格；放开的只有空白，每个字仍要按顺序出现。"""
    spaced = "[4:321:9] ( bot s Planet ) 处 于 保 护 状 态 , 我方舰队 已 返 航 。"

    assert find_protection_bounce_targets(spaced) == [Coordinate(4, 321, 9)]


def test_two_bounce_sentences_in_one_body_are_refused() -> None:
    """一封信里两句 = OCR 把两封串了，或者游戏改了版面。**不猜取第一个。**"""
    body = f"{BODY}\n[5:222:3]（y）处于保护状态，我方舰队已返航。"

    with pytest.raises(ProtectionBounceUnreadable, match="2 个"):
        read_protection_bounce(_Mail(body=body))


def test_a_header_without_a_time_is_refused() -> None:
    with pytest.raises(ProtectionBounceUnreadable, match="时间"):
        read_protection_bounce(_Mail(header="发件人: 系统\n主题: 舰队返航"))


def test_a_body_without_the_full_sentence_is_refused() -> None:
    with pytest.raises(ProtectionBounceUnreadable, match="处于保护状态"):
        read_protection_bounce(_Mail(body="[4:321:9] 我方舰队已返航。"))


def test_the_mail_list_row_classifies_as_its_own_kind() -> None:
    """列表行上那段预览文字要认得出，否则这一封根本不会被打开。"""
    assert classify_report_subject(BODY) is ReportKind.PROTECTION_BOUNCE
    assert ReportKind.PROTECTION_BOUNCE.is_dispatch_matchable is False


@pytest.mark.parametrize(
    ("subject", "kind"),
    [
        ("主题: 攻击报告", ReportKind.ATTACK),
        ("主题: 海盗攻击报告", ReportKind.PIRATE),
        ("主题: 侦察报告", ReportKind.SCOUT),
        ("主题: 你的行星被侦察", ReportKind.PLANET_SCOUTED),
        ("主题: 战报", ReportKind.SYSTEM),
    ],
)
def test_the_other_mail_kinds_are_left_alone(subject: str, kind: ReportKind) -> None:
    """新判据排在所有关键词之前，所以要证明它没抢走原有的任何一档。"""
    assert classify_report_subject(subject) is kind
