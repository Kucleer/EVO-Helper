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

    assert SCORE_RULE_VERSION == "curve/2"
    assert screen_bracket([9770.0, 9730.0]) is not None, (
        "指纹说自己是 screen-bracket 版，那区间判据就得真的在"
    )


# -- 曲线判据：参照多个历史点，不参照旁边那一行 ---------------------------------


def test_the_curve_survives_a_reference_that_is_itself_wrong() -> None:
    """⚠️⚠️ **本次改动的重点。** 历史里混进一个坏点，判据不该跟着跑偏。

    前两版都栽在「参照物是单个值」上：逐行判被上一行带跑（75.2% 的误伤），
    首尾框住这一屏被端点带跑（28% 的屏失效）。中位数抗少数坏点。
    """
    from evo_helper.domain.ranking import curve_reference

    # 上下各两个点，其中一个（3,780）是误读。真值那条线在 9,7xx 上。
    history = [(100, 9800.0), (101, 9790.0), (103, 3780.0), (104, 9770.0)]
    reference = curve_reference(history, 102)

    assert reference is not None
    assert 9_700.0 <= reference <= 9_850.0, f"一个坏点把拟合带跑了：{reference}"


def test_a_leading_digit_misread_is_caught_by_the_curve() -> None:
    """生产实况：名次 1634 读到 3,480，而邻居中位是 9,540（9 读成 3）。

    这个值**现在就在库里**——它过了所有既有判据（比上一行小、没到 5 倍断崖）。
    """
    # ⚠️ 上下都要有点：新参照不许单边外推（整段理由在 `curve_reference`）。
    history = [(1630, 9540.0), (1631, 9540.0), (1635, 9540.0), (1636, 9540.0)]

    kept = trusted_scores([3480.0], anchor=9600.0, ranks=[1634], history=list(history))

    assert kept == [None]


def test_a_real_reading_hugs_the_curve_and_is_kept() -> None:
    """真值紧贴曲线（实测中位偏差 0.71%），一个都不许误伤。"""
    history = [(1630, 9540.0), (1631, 9540.0), (1635, 9540.0), (1636, 9540.0)]

    kept = trusted_scores([9500.0], anchor=9600.0, ranks=[1634], history=list(history))

    assert kept == [9500.0]


def test_the_curve_says_nothing_until_it_has_enough_history() -> None:
    """⚠️ 点不够就**不表态**，退回原来那几条判据——不许拿一个凑出来的参照顶上。

    每趟头几屏历史本来就少，而榜首那几段恰恰是断崖最陡的地方，硬给一个参照
    只会在最不该出错的位置出错。
    """
    from evo_helper.domain.ranking import curve_reference

    assert curve_reference([(100, 9800.0), (101, 9790.0), (102, 9780.0)], 103) is None


def test_the_curve_refuses_to_extrapolate_off_one_side() -> None:
    """⚠️⚠️ **上下都要有点，单边一律不表态。**

    单边就是外推，而外推在曲率大的地方错得最狠（榜首几屏能从 5.97M 跌到十万级）。
    两侧各有点时目标夹在中间，直线假设只需要在这一小段上成立。

    ⚠️ 代价要知道：**一趟扫描最末那几行永远补不上**（它们下面还没有点）。这是有意的
    ——那几行的下一趟会被重新读到，而外推出来的错值会一直躺在库里。
    """
    from evo_helper.domain.ranking import curve_reference

    below_only = [(100, 9800.0), (101, 9790.0), (102, 9780.0), (103, 9770.0)]

    assert curve_reference(below_only, 110) is None, "往下外推了"
    assert curve_reference(below_only, 99) is None, "往上外推了"
    # 正面对照：两侧各两个点时照常表态。
    both_sides = [*below_only, (105, 9760.0), (106, 9750.0)]
    assert curve_reference(both_sides, 104) is not None


def test_the_curve_refuses_points_that_are_too_far_apart() -> None:
    """⚠️ 跨度超限就不表态。

    实测生产上有名次 273 与 821 被当成邻居（军力 10,000 vs 23,780）——隔着五百多名
    连一条直线，估出来的东西毫无意义。而「直线」这个假设只在小跨度上成立。
    """
    from evo_helper.domain.ranking import CURVE_RANK_SPAN, curve_reference

    far = [
        (100, 10_000.0),
        (101, 10_000.0),
        (100 + CURVE_RANK_SPAN + 2, 23_780.0),
        (100 + CURVE_RANK_SPAN + 3, 23_640.0),
    ]

    assert curve_reference(far, 120) is None


def test_history_only_takes_readings_the_rule_trusted() -> None:
    """⚠️ 判错的读数不许进历史——进去就会把中位数往错的方向拽，而这条链路自我强化。"""
    history: list[tuple[int, float]] = [
        (1630, 9540.0),
        (1631, 9540.0),
        (1640, 9500.0),
        (1641, 9500.0),
    ]
    trusted_scores([3480.0, 9520.0], anchor=9600.0, ranks=[1634, 1635], history=history)

    assert (1634, 3480.0) not in history, "被判错的值进了历史"
    assert (1635, 9520.0) in history, "被采信的值该进历史，否则曲线不往前走"


def test_the_curve_overrides_the_single_point_rules() -> None:
    """⚠️⚠️ 有曲线参照时**只听曲线的**，那三条单点判据不再有否决权。

    两套一起跑等于让那个可能读错的单点仍能否决——前两版栽的正是这个。
    这里锚点被压到 3,000（模拟上一屏被误判压低），而真值 9,500 贴着曲线：
    旧的 `too_big`（3,000 × 5 = 15,000）碰巧放行，但 `out_of_order` 会拦；
    曲线判据认得出它是对的。
    """
    history = [(1630, 9560.0), (1631, 9550.0), (1640, 9500.0), (1641, 9490.0)]

    kept = trusted_scores(
        [9540.0, 9530.0], anchor=3000.0, ranks=[1634, 1635], history=list(history)
    )

    assert kept == [9540.0, 9530.0]


def test_the_reference_follows_the_slope_not_just_the_level() -> None:
    """⚠️⚠️ **参照要跟着斜率走，不能只给「这一段大概什么水平」。**

    这是 `curve/2` 相对 `curve/1` 的全部改进。`curve/1` 取 ±60 名窗口的中位数，
    在斜坡上会给整个窗口同一个值，靠近两端系统性偏（实测中位误差 0.843%，而顺着
    斜率插值是 0.060%）。

    这里造一段陡坡：名次 1000 是 20,000，1100 是 15,000（用户 2026-09-03 举的那个
    例子）。目标名次 1050 的真值该在 17,500 附近；**取中位数会给出 17,500 左右也
    碰巧对**，所以目标放在 1020 —— 真值 19,000，而四个点的中位数是 17,500。
    """
    from evo_helper.domain.ranking import curve_reference

    history = [(1000, 20_000.0), (1010, 19_500.0), (1090, 15_500.0), (1100, 15_000.0)]
    reference = curve_reference(history, 1020, span=200)

    assert reference is not None
    assert 18_700.0 <= reference <= 19_300.0, (
        f"参照没跟着斜率走（{reference:,.0f}）—— 取水平值会给出约 17,500"
    )


def test_one_bad_point_cannot_drag_the_slope() -> None:
    """⚠️⚠️ **斜率取中位数，不取平均。**

    窗口里混着误读时，平均会被一个坏点整体拽偏（实测最小二乘中位误差 0.607%，
    Theil–Sen 是 0.441%），而中位数需要一半以上的点都坏才会被带跑。

    这里四个点里有一个是误读（9,000 读成 3,000）。真值那条线几乎是平的
    （9,000 → 8,970），而那个坏点会让「平均斜率」变得很陡：
    以它为端点的三条斜率都是每名次几百，平均下来把参照拽到低处。
    """
    from evo_helper.domain.ranking import curve_reference

    history = [(1000, 9_000.0), (1010, 3_000.0), (1030, 8_980.0), (1040, 8_970.0)]
    reference = curve_reference(history, 1020, span=200)

    assert reference is not None
    assert 8_900.0 <= reference <= 9_100.0, (
        f"一个坏点把斜率带跑了（{reference:,.0f}）—— 中位数不该被单点影响"
    )


def test_the_fourteen_percent_misreads_that_curve_one_let_through_are_caught() -> None:
    """⚠️⚠️ **容差必须收到 3%，35% 拦不住生产上真实漏过去的那一批。**

    `curve/1` 用 35% 容差，实测放行了这些（都躺在生产库里，是 2026-09-03 留一验证
    从邻居反推出来的）：

        名次 1604–1606  读到 5,970   邻居 6,980 / 6,950   偏 14.5%   6→5
        名次 1538       读到 6,330   邻居 8,3xx           偏 24.1%   8→6
        名次  849       读到 15,280  邻居 19,3xx          偏 20.7%   9→5（第二位）

    偏离全在 35% 之下。而 `curve/2` 的参照精度好了一个数量级（中位 0.112%），
    所以 3% 收得住而不误伤：真值的 P90 只有 1.97%。

    ⚠️ 这一条不许换成一个偏离 60% 的例子 —— 那种 35% 也拦得住，用例就白写了
    （2026-09-03 变异验证当场抓到：把容差改回 35%，其余用例全绿）。
    """
    # 名次 1604 那一组：真值该在 6,970 附近，读到 5,970。
    history = [(1602, 6_980.0), (1603, 6_980.0), (1607, 6_950.0), (1608, 6_950.0)]

    kept = trusted_scores([5_970.0], anchor=7_000.0, ranks=[1604], history=list(history))

    assert kept == [None], "偏 14% 的误读没被拦下——容差是不是又放宽了？"

    # 反面：同一组邻居下，偏 2% 的真实波动照常留住。
    fine = trusted_scores([6_840.0], anchor=7_000.0, ranks=[1604], history=list(history))
    assert fine == [6_840.0], "偏 2% 的真值被误伤了——容差是不是收得太紧？"


def test_the_tolerance_is_three_percent_not_ten() -> None:
    """⚠️ 容差定在 **3%**（用户口径 2026-09-03：「我要3%」），不是 10%。

    ## 这一条钉的是一个**决策**，证据强度要说清楚

    上面那条用例只钉住了「≥14% 拦下、≤2% 放行」——3% 和 10% 之间没有区分（变异验证
    当场发现：把容差改成 10%，其余用例全绿）。而那一段本身就是混的：

        真值的偏差    P90 1.97%   P95 7.80%
        5–8% 那一档   16 个点，抽查里约 73% 是确凿误读

    也就是说 5% 附近**不是一条自然分界**，选 3% 是成本权衡的结果：误杀一个好值只是
    把它换成偏差约 0.1% 的估算（几乎无代价），而漏过一个误读会让库里躺着偏 14–24%
    的军力值并直接进选靶排序。

    ⚠️ 所以这一条断言的是「**5% 的偏差按误读处置**」——它有 73% 的把握，不是 100%。
    要是哪天决定放宽到 10%，改这条用例就是了；但**别不改用例就改常量**，那样这个
    决策会悄悄消失。
    """
    history = [(1602, 10_000.0), (1603, 10_000.0), (1607, 10_000.0), (1608, 10_000.0)]

    at_five = trusted_scores([9_500.0], anchor=10_100.0, ranks=[1604], history=list(history))

    assert at_five == [None], "偏 5% 的读数没被拦下——容差是不是放宽到 5% 以上了？"
