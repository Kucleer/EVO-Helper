"""导航栏回读的离线复盘台。**这是量具，量具错了比没有更糟。**

2026-08-25 这个工具第一版就把要测的东西过滤掉了：置信度判据只写了「所有读数都能
解释成真值漏字」，于是 `15 ← ['6','1','15','15','15']` 因为那个替换型的 `6` 被划进
「低置信、不并进结论」——而它恰恰是那次改动要救的主力形态。**报表照样是绿的，
结论却少了 123 个格子。**这个文件钉的就是这一类。
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path

from evo_helper.tools.nav_readback_replay import (
    Cell,
    cells,
    rule_2026_08_18,
    rule_live,
    rule_plurality,
    rule_promote_longest,
    score,
)


def _cell(wanted: str, reads: tuple[str, ...]) -> Cell:
    return Cell(log_id=1, at="", box="system", reads=reads, wanted=wanted)


# -- 置信度分档 ------------------------------------------------------------------


def test_a_substitution_does_not_push_a_cell_out_of_the_confident_tier() -> None:
    """⚠️⚠️ **有配方一字不差读出真值时，别的读数怎么错都算高置信。**

    这就是第一版的缺陷：`6` 不是 `15` 漏字后的样子，于是整格被划走。而三套配方
    读出了完整的 `15`——屏上写着别的数、却被三套配方一致读成 `15`，这种巧合排除得掉。

    少了这一条，复盘台会把**要测的那一类样本**过滤掉，然后报出一份好看的空表。
    """
    assert _cell("15", ("6", "1", "15", "15", "15")).confident is True


def test_a_cell_where_every_read_is_a_drop_is_confident_too() -> None:
    """没有一套读全，但每个读数都能解释成漏字 —— 也算高置信。

    生产 `117 ← ['','7','7','7','7']` 就是这个形状：`117` 一次都没被读出来，
    可 `7` 是它漏了两位的样子，说明屏上确实是 `117`。
    """
    assert _cell("117", ("", "7", "7", "7", "7")).confident is True


def test_a_cell_that_looks_like_a_different_number_is_not_confident() -> None:
    """⚠️ 反过来：读数既不等于真值、也解释不成它漏字 —— 这才是「导航栏可能真的
    停在别处」，不能拿它给规则打分。

    ⚠️ 这一条不能删。2026-08-25 那批数据里 1290 格全部落在高置信档，于是这道闸
    **一次都没打着**——但它是唯一能把「我们读错了」和「导航栏跑偏了」分开的东西。
    没有它，哪天导航栏真跑偏，复盘台会把那些格子当成 OCR 失败去调规则。
    """
    assert _cell("15", ("83", "83", "83", "", "")).confident is False


# -- 摊平 ------------------------------------------------------------------------


def test_all_three_boxes_are_collected_not_just_the_failing_one() -> None:
    """⚠️ 三个框全都要收。

    一条规则的代价一半在「本来对的格子被它弄错了几个」，而那些格子就在同一条
    日志里躺着。只收判错的那个框，这一半会结构性地看不见——于是一条更爱猜的规则
    看起来只有收益、没有代价。
    """
    got = cells(
        [
            {
                "id": 1,
                "expected": "4:277:15",
                "reads": {
                    "galaxy": ["4", "4", "4", "4", "4"],
                    "system": ["277", "277", "277", "77", "77"],
                    "position": ["6", "1", "15", "15", "15"],
                },
            }
        ]
    )

    assert [cell.box for cell in got] == ["galaxy", "system", "position"]
    assert [cell.wanted for cell in got] == ["4", "277", "15"]


def test_a_payload_without_a_usable_coordinate_is_skipped() -> None:
    """`expected` 不是三段坐标就跳过 —— 老日志、手工插的行都可能长成别的样子。

    宁可少几条也不许对错位：真值错位会让打分表整个失去意义，而它不会报错。
    """
    assert cells([{"id": 1, "expected": "", "reads": {"galaxy": ["4"]}}]) == []


# -- 基线必须冻住 ----------------------------------------------------------------


def test_the_frozen_baseline_does_not_follow_the_live_rule() -> None:
    """⚠️⚠️ **基线是抄下来的一份，不许跟着 `agreed_value` 变。**

    `rule_2026_08_18` 存在的唯一意义是回答「换成新规则之后好了多少」。它一旦
    改成 `from ... import agreed_value`，「较基线救回」永远是 0，而表还是绿的
    ——一个恒等于零的量具比没有量具更糟，因为它看起来在工作。

    这一条钉的是那条老规则的招牌行为：真值 `15`，三票稳赢，被一个替换型的 `6`
    一票否决。**现在跑的规则在这里交 `15`**，两者必须不同。
    """
    assert rule_2026_08_18(("6", "1", "15", "15", "15")) == ""


# -- 墓碑 ------------------------------------------------------------------------


def test_the_tombstone_rule_really_does_break_the_case_it_is_kept_for() -> None:
    """⚠️ 「提升到更长读数」那条留着当反面教材，而反面教材要真的反。

    两组读数的投票形态完全一样（2 票短值 + 1 票长值 + 碎片），一个是漏字、
    一个是多字：`261` 上它对，`391` 上它把本来能读对的值改成 `3931`。

    这一条不是装饰：它证明「光看读数分不开漏字和多字」——也就是为什么
    「正确值票数不够」那一类必须等下一步（数屏上有几位数字）才救得回来。
    哪天有人删掉这个墓碑去重新发明它，得先让这条红。
    """
    assert rule_promote_longest(("261", "26", "26", "6", "61")) == "261"
    assert rule_promote_longest(("3", "3931", "391", "331", "391")) == "3931"


def test_plain_plurality_trades_blanks_for_wrong_answers() -> None:
    """⚠️ 「谁票多认谁」在同一组读数上交出 `26` —— 一个**可能缺了位的坐标**。

    钉住它是为了让打分表上那 134 个「交空 → 读错」有个出处。
    """
    assert rule_plurality(("261", "26", "26", "6", "61")) == "26"


# -- 打分 ------------------------------------------------------------------------


def test_blank_and_wrong_are_counted_apart() -> None:
    """⚠️⚠️ **「交空」和「读错」不许并成一档。**

    交空只是下一个目标多重设一个字段；读错是拿一个可能不对的坐标去确认，
    代价见 `SystemNavigator` 类注释里 136→9 那次事故。并成「不对」会让一条更爱猜
    的规则看起来和一条更谨慎的一样好——而 2026-08-25 选规则时，这两栏的差别
    正是唯一的取舍点。
    """
    group = [_cell("15", ("15", "15", "")), _cell("261", ("26", "26", "")), _cell("9", ("", ""))]

    result = score(rule_plurality, group)

    assert (result.right, result.wrong, result.blank) == (1, 1, 1)


# -- 拿生产读数当回归基准 ---------------------------------------------------------

SNAPSHOT = Path(__file__).resolve().parents[2] / "fixtures" / "nav_readback_2026-08-25.json"


def _snapshot() -> list[tuple[int, Cell]]:
    raw = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return [
        (
            item["count"],
            Cell(
                log_id=0,
                at="",
                box=item["box"],
                reads=tuple(item["reads"]),
                wanted=item["wanted"],
            ),
        )
        for item in raw["样本"]
    ]


def _tally(rule: Callable[[Sequence[str]], str]) -> dict[str, int]:
    out = {"对": 0, "空": 0, "错": 0}
    for count, cell in _snapshot():
        got = rule(cell.reads)
        out["对" if got == cell.wanted else ("空" if not got else "错")] += count
    return out


def test_the_snapshot_is_the_size_this_file_talks_about() -> None:
    """语料规模自己说一遍 —— 下面几条断言的数字都是按它算的。"""
    raw = json.loads(SNAPSHOT.read_text(encoding="utf-8"))

    assert raw["值框格数"] == 1290
    assert sum(count for count, _cell in _snapshot()) == 1290


def test_the_live_rule_beats_the_old_one_on_real_production_reads() -> None:
    """⚠️⚠️ **窄化否决在 1290 个真实值框上的成绩，逐个数字钉住。**

    这是 2026-08-25 那次改动的**全部依据**，所以它必须是一条会红的用例，而不是
    提交信息里的一句话。数字来自 `tools.nav_readback_replay` 跑生产
    `system_log` 2026-08-19 → 08-25 的 430 条告警。

    ⚠️ **「读错」那一栏一步都不许涨。** 交空只是下一个目标多重设一个字段；
    读错是拿一个可能不对的坐标去确认。当初选规则时，「众数」多读对 160 个但多
    读错 134 个、「众数+提升」多读对 257 个但多读错 37 个——两条都被这一栏否掉了。
    """
    old = _tally(rule_2026_08_18)
    new = _tally(rule_live)

    assert (old["对"], old["空"], old["错"]) == (831, 361, 98)
    assert (new["对"], new["空"], new["错"]) == (954, 238, 98)
    assert new["错"] == old["错"], "多读错了 —— 这条链路宁可交空也不许猜"


def test_every_cell_the_new_rule_changed_moved_from_blank_to_right() -> None:
    """⚠️ 改变判决的 123 个格子**全部**是「交空 → 读对」，一个反向的都没有。

    比上一条更强：那条只对得上总数，而总数可以由「救回 130、弄坏 7」凑出来。
    这条逐格看方向。
    """
    moves = Counter()
    for count, cell in _snapshot():
        was, now = rule_2026_08_18(cell.reads), rule_live(cell.reads)
        if was != now:
            verdict = lambda text: "对" if text == cell.wanted else ("空" if not text else "错")  # noqa: E731
            moves[f"{verdict(was)}→{verdict(now)}"] += count

    assert dict(moves) == {"空→对": 123}


def test_the_rules_that_lost_are_kept_scored_so_the_trade_off_stays_visible() -> None:
    """⚠️ 落选的两条也钉住成绩 —— 否则「为什么没选更激进的那条」只剩一句口头解释。

    哪天有人想放宽判据，这里直接告诉他要付什么：多读对多少、多读错多少。
    """
    assert _tally(rule_plurality) == {"对": 991, "空": 67, "错": 232}
    assert _tally(rule_promote_longest) == {"对": 1088, "空": 67, "错": 135}
