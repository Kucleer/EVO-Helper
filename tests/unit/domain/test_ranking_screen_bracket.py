"""屏内首尾框住这一屏：止住「一行读错、后面整段陪葬」的级联误伤。

## 生产实况（2026-09-02，近 3 天）

`trusted_scores` 原先只有一条降序判据：**比屏内上一个可信值大就丢**。它防得住
「读大了」，防不住「读小了却仍然递减」——那种错读顺利通过判据、当上基准，然后把
它后面每一行正确的值都判成逆序。

    真值    9770  9760  9750  9740  9730
    读成    9770  3760  9750  9740  9730      ← 只有第 2 行读错（9 → 3）
    判据    ✓     ✓     ✗     ✗     ✗        ← 基准掉到 3760，后面全陪葬

被丢 ≥2 行的 1,099 组里：

    整组自身完美降序   867 组   78.9%    ← 被丢的那些行彼此严格递减
    落在其中的行       3,827 / 5,090 = 75.2%
    那些组的组内跨度    中位 0.31%

**一批彼此严格递减、总跨度 0.3% 的读数不可能是读错了。** 另外两个指标同样指向
级联：被丢的值 71.5% 其实 ≤ 锚点（是真值），末行被丢的次数是首行的 7 倍。

用户口径（2026-09-02，逐字）：

    「只要该屏内首尾能读出，并且与上一屏保持递减，我能接受全部的估算值」
    「所有这几行 首尾在的话，不应该被丢弃啊」

## 这个文件钉的是「什么时候敢放宽」，不是「放宽之后一切都信」

区间只有在**首尾自己站得住**时才作数；站不住就退回原来那条逐行判据，行为逐字不变。
而数量级误读（丢首位 / 多一位）始终由 `SCORE_CLIFF_FACTOR` 那两条独立挡着，
**跟这个区间无关**——两套阈值各管一件事。
"""

from __future__ import annotations

from evo_helper.domain.ranking import (
    SCREEN_SPREAD_LIMIT,
    screen_bracket,
    trusted_scores,
)

# -- 级联误伤：本次要治的那个病 -------------------------------------------------


def test_one_low_misread_no_longer_drags_the_rest_of_the_screen_down() -> None:
    """⚠️⚠️ **本文件的重点。** 一行读小了，后面那些正确的值必须留得住。

    这就是生产上 75.2% 被丢行的成因。旧判据把 `3760` 当上基准，于是 9750/9740/9730
    全成了「破坏降序」——而它们一个字都没读错。
    """
    read = [9770.0, 3760.0, 9750.0, 9740.0, 9730.0]

    kept = trusted_scores(read, anchor=9800.0)

    assert kept == [9770.0, None, 9750.0, 9740.0, 9730.0], (
        "读小了的那一行该丢，它后面正确的三行必须留住"
    )


def test_the_misread_row_itself_is_still_dropped() -> None:
    """⚠️ 放宽的是**被它连累的那些**，不是它自己。

    `3760` 落在 `[9730, 9770]` 之外，照丢。区间判据不是「不判了」。
    """
    kept = trusted_scores([9770.0, 3760.0, 9750.0], anchor=9800.0)

    assert kept[1] is None


def test_a_row_read_too_high_in_the_middle_is_dropped_too() -> None:
    """区间是双向的：读大了同样出界。旧判据只挡得住这一个方向。"""
    kept = trusted_scores([9770.0, 9900.0, 9750.0], anchor=9800.0)

    assert kept == [9770.0, None, 9750.0]


# -- 什么时候区间不作数 ----------------------------------------------------------


def test_a_screen_whose_ends_do_not_descend_falls_back() -> None:
    """⚠️ 首尾自己就不递减 → 至少一个端点是错的 → 区间不作数，退回逐行判据。"""
    assert screen_bracket([9000.0, 9500.0]) is None


def test_a_screen_whose_ends_are_too_far_apart_falls_back() -> None:
    """⚠️⚠️ **没有这一条，改动比不改还糟。**

    末行丢了首位（真值 9,740 读成 1,740）时，下界会被拉到 1,740，于是整屏连同真正
    的错读一起放行。实测真实跨度中位 0.31%、P90 4%，而端点被数量级读错时跳到 99%。
    """
    assert screen_bracket([9740.0, 5000.0, 1740.0]) is None
    # 刚好在限内的照常作数。
    assert screen_bracket([9740.0, 9000.0, 9740.0 / SCREEN_SPREAD_LIMIT]) is not None


def test_a_screen_with_only_one_readable_score_falls_back() -> None:
    """一个点框不出区间。"""
    assert screen_bracket([None, 9740.0, None]) is None


def test_a_head_that_towers_over_the_screen_falls_back() -> None:
    """首行比末行高出一个数量级 → 端点自己就错了 → 区间不作数（跨度那道闸）。"""
    assert screen_bracket([93670.0, 9650.0]) is None


def test_a_slightly_low_anchor_does_not_disable_the_bracket() -> None:
    """⚠️⚠️ **区间不看锚点，卡「首 ≤ 锚点」是有害的。**

    用户口径那句话的第二半「与上一屏保持递减」**已经由 `too_big` / `too_small` 守着**
    （它们拿锚点当基准逐行判），区间只替换 `out_of_order` 那一条。

    在 `screen_bracket` 里再卡一次锚点：松着卡是**死代码**（变异验证过，删掉全绿）；
    严着卡（「首 ≤ 锚点」）是**有害的**——锚点自己可能被上一屏误判压低，这里构造的
    就是生产实测那一组（锚点 25190、真值 29130），严卡会让整屏退回逐行判据，
    把跨屏级联换个地方再犯一次。
    """
    assert screen_bracket([29130.0, 29110.0]) is not None
    kept = trusted_scores([29130.0, 29110.0, 29110.0], anchor=25190.0)
    assert kept == [29130.0, 29110.0, 29110.0], "偏低的锚点不该让这一屏的好读数出局"


def test_falling_back_keeps_the_old_behaviour_word_for_word() -> None:
    """区间不作数时，逐行降序判据原样生效——放宽只在证据齐全时发生。"""
    read = [9740.0, 9750.0, 9730.0]  # 首尾不递减 → 无区间

    assert trusted_scores(read, anchor=9800.0) == [9740.0, None, 9730.0]


# -- 数量级那两条不受影响 --------------------------------------------------------


def test_the_ten_times_guard_still_fires_inside_a_valid_bracket() -> None:
    """⚠️ 丢首位 / 多一位那两条由 `SCORE_CLIFF_FACTOR` 独立挡着，**跟区间无关**。

    这里首尾（9770 / 9730）框得好好的，中间那个 93,700 仍旧要被 `too_big` 挡下——
    它同时也在区间外，两条判据都该说不。少了这条用例，有人把断崖判据删掉时，
    只有区间那一层还在，而区间的上界正好来自可能被读错的首行。
    """
    kept = trusted_scores([9770.0, 93700.0, 9730.0], anchor=9800.0)

    assert kept == [9770.0, None, 9730.0]


def test_a_whole_screen_read_ten_times_too_small_is_still_caught() -> None:
    """整屏偏小 10 倍时屏内自成完好降序、区间也自洽——**只有锚点那条挡得住**。

    这正是 #251 记的那个漏网（`11.75K` 读成 `1.75K`，整整三屏）。区间判据一个字都
    没放松它：`too_small` 拿锚点当基准，与首尾无关。
    """
    kept = trusted_scores([1750.0, 1600.0, 1412.0], anchor=13200.0)

    assert kept == [None, None, None]


# -- 判据要说得出「为什么」 -------------------------------------------------------


def test_every_dropped_row_says_which_rule_dropped_it() -> None:
    """⚠️⚠️ **四条判据各有各的处置，日志必须分得开。**

    2026-09-02 查这件事时最费劲的一步就是这个：日志只说「不可信」，只能拿
    「值 ÷ 锚点」去反推是哪一条——而 71.5% 的被丢值其实 ≤ 锚点，那个比值什么都
    说明不了。理由现在由判据自己交出来。
    """
    from evo_helper.domain.ranking import (
        DROP_OUT_OF_BRACKET,
        DROP_TOO_BIG,
        score_drop_reasons,
    )

    read = [9770.0, 3760.0, 9750.0]
    why = score_drop_reasons(read, anchor=9800.0)

    assert why == [None, DROP_OUT_OF_BRACKET, None], "可信的位置必须是 None"
    assert score_drop_reasons([9770.0, 93700.0, 9730.0], anchor=9800.0)[1] == DROP_TOO_BIG, (
        "数量级错了就该报数量级，不该报「出界」——两者的处置完全不同"
    )


def test_the_reasons_line_up_with_what_was_actually_dropped() -> None:
    """⚠️⚠️ **值和理由必须出自同一次遍历。**

    拆成两个函数各判一遍就是「同一件事两份实现」，两边迟早分家——而分家之后日志会
    **理直气壮地说错话**，比不记更糟。这一条把两者钉在一起：`None` 的位置必须严格
    对应可信的位置。
    """
    from evo_helper.domain.ranking import score_drop_reasons, trusted_scores

    read = [9770.0, 3760.0, 9750.0, 93700.0, 9730.0, None]
    kept = trusted_scores(read, anchor=9800.0)
    why = score_drop_reasons(read, anchor=9800.0)

    assert len(kept) == len(why) == len(read)
    for value, reason in zip(kept, why, strict=True):
        assert (value is None) == (reason is not None) or value is None, (
            "留住的行不许带理由，丢掉的行必须有理由"
        )


def test_the_rule_version_is_a_fingerprint_only_this_version_writes() -> None:
    """⚠️ 版本指纹跟着判据走，改了判据就得改它。

    仓库里有过教训：#260 与 #262 两版的 `criterion` 一字不差，最准的那条指纹分不出
    它们，只能退回去看行为。这一条把指纹和判据钉在一起——区间那道闸还在，指纹就得
    还是这一版。
    """
    from evo_helper.domain.ranking import SCORE_RULE_VERSION

    assert SCORE_RULE_VERSION == "screen-bracket/1"
    assert screen_bracket([9770.0, 9730.0]) is not None, (
        "指纹说自己是 screen-bracket 版，那区间判据就得真的在"
    )
