"""导航栏三个**值框**的 ROI、配方与汇总规则，跑在真实截图上。

## ⚠️ 为什么语料和真值都不在仓库里

本仓是**公开仓库**，而值框里写的就是坐标（`.gitignore` 第二段写着 2026-08-18 那次
把 34 份战报面板当夹具提交、只能整个撤回的事）。所以图和真值都放 `var/` 下，
本机有才跑，CI 里跳过；**committed 的这份只断言统计量**，不写任何一个具体坐标。

## ⚠️⚠️ 原先那 43 张语料**已经不存在了**（2026-08-25 查明）

这个文件从前说自己跑在 43 张上。实际上 `var/fixtures/vision/nav_bar_values.json`
在任何一台还找得到的机器上都没有，`var/logs` 里也没有那批图——**这条用例一直在
skip**，而且不知道 skip 了多久。

后果不是「少跑了几条用例」：2026-08-25 生产读数证伪了「每套配方错法只有丢位」
那条性质，而本机**没有任何东西因此变红**，因为守着它的用例根本没在跑。

## 现在这份：9 张，从生产实机取回（2026-08-25）

用户从实机 `var/logs` 取回的 226 张 dump 里，停在恒星系视图、三个值框读得出的
只有 `dump-bot-coord-mismatch-*` 与 `dump-preset-not-found-*` 这两类，共 9 张。
真值逐张放大目视核对。

⚠️ **另有两张 `184800`／`184824` 没有收**：它们和 `184737` 是同一个坐标、只是被
浮层压暗了。收进来会把语料从 9 张「涨」到 11 张，而多出来的两张一个新字形都不带
——**用近似重复把语料撑大，正是当年「九张里八张都是 137」那个坑的另一种形状**。

## 这份语料头一次证明了什么

**「每套配方错法只有丢位」在真像素上是假的。** 135 次读屏里有 6 处替换或凭空多位，
分布在五套里的三套：

    2x/th140/tight   9  → 3        1x/th170/tight   95 → 35
    1x/th170/tight   15 → 6        2x/th140/tight  297 → 237
    3x/th170/tight  297 → 37       3x/th170/tight  189 → 1893

其中 **`15 → 6` 正是生产上那条**（`['6','1','15','15','15']`）—— 从前只在生产日志
的文字读数里见过，现在有了对应的真像素。

## 这里守的是什么

1. `MISREAD_CELLS` —— **必须是 0，永远只能是 0**。这是整份文件的承重墙。
2. `PERFECT_SHOTS` / `UNREADABLE_CELLS` —— 成绩，两个方向都不许悄悄变。
3. `SUBSTITUTING_READS` —— 上面那 6 处。**它不是 0，也不假装是 0**；
   重挑配方时这个数要降，那才是「配方变好了」的判据。

## ⚠️⚠️ 这份语料只有成功样本，**它证明不了实机上不出错**

9 张里没有 `117`／`261`／`391` 这几个生产上天天错的字形。这份语料回答的是
「本来就读得对的格子会不会被弄坏」，另一半在 `tools.nav_readback_replay`——
它拿几百条**生产失败读数**给候选规则打分。

**两边都要过。改配方或改汇总规则时只跑一边，就是把这个坑再踩一次。**

字形还在攒：`pirate_loop._value_box_evidence` 每遇到一种没见过的读数形态就把
三个值框按原分辨率落库，`tools.nav_value_corpus` 把它们捞成这里能用的语料。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evo_helper.game.system_navigator import (
    NAV_VALUE_MIN_VOTES,
    NAV_VALUE_RECIPES,
    NAV_VALUE_ROIS,
    agreed_value,
)

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")

SHOTS = Path("var/logs")
TRUTH_FILE = Path("var/fixtures/vision/nav_bar_values.json")

#: 语料份数（9 张 × 3 个值框 = 27 个格子），2026-08-25 从实机取回那一批，全部人工核过。
#:
#: ⚠️ **这个数只该因为「攒到了新字形」而涨。** 拿近似重复（同一坐标、只是浮层压暗）
#: 把它撑大，等于回到「九张里八张都是 137」那个坑——数字好看了，覆盖一点没变。
CORPUS_SIZE = 9

#: 三个格子全读对的份数。
#:
#: ⚠️ **这个数是判据，两个方向都不许悄悄变。** 涨上去说明识别变准了（好事，改大），
#: 掉下来说明有东西回归了。
#:
#: 2026-08-25 窄化否决判据之后，这批语料上按格子算是 **21 对 / 6 空 / 0 错**；
#: 老判据是 19 / 8 / 0。救回的两格是 `95`（被那个 `35` 一票否决）和
#: `15`（被那个 `6` 一票否决）——**和生产上救回的 123 格是同一种成因**。
PERFECT_SHOTS = 5

#: 汇不拢、交空串的格子数（27 格里 4 格）。空串走「读不通就不确认」那一支，
#: 代价只是下一个目标白设两个字段。**这是承认，不是豁免**，四格各自的成因：
#:
#:     9   ← ['', '3', '', '', '']                 孤证且无旁证 —— 见 `_needs_a_second_recipe`
#:     11  ← ['', '1', '', '', '']    （两张）      屏上 2 位，唯一的读数只有 1 位
#:     297 ← ['297','237','27','37','97']          过完位数闸剩 297 与 237，各一票，裁不了
#:
#: ⚠️ 2026-08-25 加位数判据之前是 6 格。救回的两格是 `12`（`['','12','2','','']`，
#: 孤证但 `2` 是它漏字后的样子）和 `189`（`['189','189','189','1893','189']`，
#: 那个凭空多一位的 `1893` 被位数闸挡掉，剩下四票一致）。
UNREADABLE_CELLS = 4

#: 读错的格子数。**必须是 0，而且永远只能是 0。**
#:
#: ⚠️ 这一条是整份文件的承重墙。读空只是白设两个字段，读错要付的是缓存与导航栏
#: 分岔——`SystemNavigator` 类注释里那次 136→9，连续 44 个目标核对全不过、13 分钟
#: 一发没派。
MISREAD_CELLS = 0

#: 阈值两侧至少要留这么多余量，见 `test_the_ink_threshold_keeps_room_on_both_sides`。
DIGIT_THRESHOLD_MARGIN = 40

#: 单套配方读出「替换或凭空多一位」的次数（27 格 × 5 套 = 135 次读屏里 6 次）。
#:
#: ⚠️⚠️ **这个数不是 0，也不许假装是 0。** 这里从前有一条断言它必须是 0 的用例，
#: 依据是「池子里每套配方错法只有丢位」——`agreed_value` 最后一条判据的安全性
#: 整个架在那句话上。2026-08-25 生产读数否掉了它，而这份语料头一次在**真像素**上
#: 复现了：
#:
#:     2x/th140/tight   9  → 3        1x/th170/tight   95 → 35
#:     1x/th170/tight   15 → 6        2x/th140/tight  297 → 237
#:     3x/th170/tight  297 → 37       3x/th170/tight  189 → 1893
#:
#: 五套里有三套会替换。判据已经窄化，不再依赖那条性质。
#:
#: 留着这个数是因为它是**重挑配方唯一的靶子**：降下去才叫配方变好了。
#: 而「把它断言成 0」只会让人再一次挑一批「在手上这点语料里恰好不出错」的配方。
SUBSTITUTING_READS = 6

pytestmark = pytest.mark.skipif(
    not (SHOTS.exists() and TRUTH_FILE.exists()),
    reason=f"缺实拍语料（{SHOTS}/*.png 与 {TRUTH_FILE}），本机没有就跳过",
)


@pytest.fixture(scope="module")
def ocr():  # type: ignore[no-untyped-def]
    from evo_helper.tools.scan_coordinates import make_ocr

    return make_ocr()


@pytest.fixture(scope="module")
def truth() -> dict[str, tuple[str, str, str]]:
    loaded = json.loads(TRUTH_FILE.read_text(encoding="utf-8"))
    return {name: tuple(values) for name, values in loaded.items()}  # type: ignore[misc]


@pytest.fixture(scope="module")
def reads(ocr, truth):  # type: ignore[no-untyped-def]
    """`{文件名: (每个框的每套配方读数, ...)}`。整批只跑一次——129 格 × 5 套 OCR。"""
    table: dict[str, tuple[tuple[str, ...], ...]] = {}
    for name in truth:
        path = SHOTS / name
        if not path.exists():
            pytest.skip(f"语料缺 {path}")
        image = Image.open(path)
        table[name] = tuple(
            tuple(
                ocr(image.crop(roi), digits=True, upscale=upscale, threshold=threshold, tight=tight)
                for upscale, threshold, tight in NAV_VALUE_RECIPES
            )
            for roi in NAV_VALUE_ROIS
        )
    return table


@pytest.fixture(scope="module")
def counted(truth):  # type: ignore[no-untyped-def]
    """`{文件名: (每个框数出来的位数, ...)}` —— `digits_on_screen` 在真像素上的成绩。"""
    from evo_helper.game.system_navigator import digits_on_screen

    return {
        name: tuple(digits_on_screen(Image.open(SHOTS / name).crop(roi)) for roi in NAV_VALUE_ROIS)
        for name in truth
    }


def _adopted(reads, counted, name: str, index: int) -> str:  # type: ignore[no-untyped-def]
    """⚠️ 走**线上真正那条路**：位数闸 + 裁决。

    从前这里只调 `agreed_value(reads)`，于是位数判据上线之后这个文件量的仍是
    没有它的成绩——「守着的东西」和「跑着的东西」是两回事，而这正是这条链路
    反复付账的那一类漏子。
    """
    return agreed_value(reads[name][index], digits=counted[name][index])


def test_the_digit_count_is_right_on_every_cell(counted, truth) -> None:  # type: ignore[no-untyped-def]
    """⚠️⚠️ **位数必须逐格数对。** 这是整个位数判据的地基。

    数错的两个方向都会出事，而且方向相反：

    - **少数**（相邻数字粘成一块）→ `261` 数成 2 位就会采纳截断的 `26`，一个缺了位
      的坐标；
    - **多数**（一个数字裂成两块）→ `391` 数成 4 位反而正好配上某套配方臆造的
      `3931`。

    所以 `NAV_DIGIT_INK_THRESHOLD` 取的是实测安全区间的**正中**，不偏向任何一边。
    """
    wrong = [
        f"{name} 第 {index} 格：真值 {want}（{len(want)} 位）数成了 {counted[name][index]} 位"
        for name, wanted in truth.items()
        for index, want in enumerate(wanted)
        if counted[name][index] != len(want)
    ]

    assert wrong == [], wrong


def test_the_ink_threshold_keeps_room_on_both_sides(truth) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 阈值不能只是「在这批语料上恰好行」，两边都要留出余量。

    逐格算出「数得对」的阈值区间，取交集。2026-08-25 在 48 格上量到的公共区间是
    **[130, 236]**（21 张生产裁片 + 这里 27 格），而 `NAV_DIGIT_INK_THRESHOLD = 180`
    落在正中，两侧各有 50 上下。

    这一条钉的是**余量**而不是那个数本身：哪天新语料把区间挤窄了，它会先红，
    那时该重新标定，而不是把常量往边上挪一点糊过去。
    """
    from evo_helper.game.system_navigator import NAV_DIGIT_INK_THRESHOLD, digits_on_screen

    low, high = 0, 255
    for name, wanted in truth.items():
        image = Image.open(SHOTS / name)
        for roi, want in zip(NAV_VALUE_ROIS, wanted, strict=True):
            crop = image.crop(roi)
            import evo_helper.game.system_navigator as nav

            ok = []
            for value in range(100, 251, 2):
                nav.NAV_DIGIT_INK_THRESHOLD = value
                if digits_on_screen(crop) == len(want):
                    ok.append(value)
            nav.NAV_DIGIT_INK_THRESHOLD = NAV_DIGIT_INK_THRESHOLD
            low, high = max(low, min(ok)), min(high, max(ok))

    assert (
        low + DIGIT_THRESHOLD_MARGIN <= NAV_DIGIT_INK_THRESHOLD <= high - DIGIT_THRESHOLD_MARGIN
    ), f"安全区间只剩 [{low}, {high}]，而阈值是 {NAV_DIGIT_INK_THRESHOLD}"


def test_the_corpus_is_the_size_this_file_talks_about(truth) -> None:  # type: ignore[no-untyped-def]
    """底下几个数都是按这批语料量的；语料换了，那几个数就得重量。"""
    assert len(truth) == CORPUS_SIZE


def test_the_pooled_reading_never_gets_a_number_wrong(reads, counted, truth) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **一个格子都不许读错。** 读空可以，读错不行。

    这正是老版本失守的地方：老注释断言「只会读空不会读错」，实机上却把
    `277` 读成 `77`。这条用真语料把那句话钉住。
    """
    wrong = []
    for name, wanted in truth.items():
        for index, want in enumerate(wanted):
            got = _adopted(reads, counted, name, index)
            if got and got != want:
                # 报错里只说形状，不抄坐标——本仓是公开仓库。
                wrong.append(f"{name} 第 {index} 格：读出 {len(got)} 位，真值 {len(want)} 位")
    assert len(wrong) == MISREAD_CELLS, wrong


def test_the_pooled_reading_gets_this_many_shots_completely_right(reads, counted, truth) -> None:  # type: ignore[no-untyped-def]
    """三格全对的份数。掉下来就是回归，涨上去把常量改大。"""
    perfect = sum(
        1
        for name, wanted in truth.items()
        if tuple(_adopted(reads, counted, name, index) for index in range(len(wanted)))
        == tuple(wanted)
    )
    assert perfect == PERFECT_SHOTS


def test_the_cells_that_stay_unreadable_are_counted_not_hidden(reads, counted, truth) -> None:  # type: ignore[no-untyped-def]
    """读不出来的格子有几个，明写出来。**这是承认，不是豁免。**"""
    blank = sum(
        1
        for name, wanted in truth.items()
        for index in range(len(wanted))
        if not _adopted(reads, counted, name, index)
    )
    assert blank == UNREADABLE_CELLS


def test_the_substituting_reads_are_counted_not_wished_away(reads, truth) -> None:  # type: ignore[no-untyped-def]
    """⚠️⚠️ **单套配方会替换、会凭空多一位。数出来，别断言它是 0。**

    这里从前是 `test_every_recipe_in_the_pool_only_ever_drops_digits`，断言
    「池子里每套配方错法只能是漏字」。`agreed_value` 最后一条判据的安全性整个架在
    那句话上——而**它是假的**。2026-08-25 生产读数先否掉了它，这份语料随后在真像素上
    复现：135 次读屏里 6 次替换或多位，分布在五套里的三套。

    其中 `15 → 6` 正是生产上那条（`['6','1','15','15','15']`）。

    ⚠️ **为什么改成数而不是继续断言 0。** 那条断言从前一直是绿的，不是因为性质成立，
    是因为语料里恰好没有会出错的字形——绿灯来自盲区。把它留成 0 只会让下一个人
    再挑一批「在手上这点语料里恰好不出错」的配方，第三次踩同一个坑。

    这个数是**重挑配方唯一的靶子**：降下去才叫配方变好了。所以两个方向都钉住——
    涨了是回归，降了是好事、连同 `SUBSTITUTING_READS` 一起改小。
    """
    from evo_helper.game.system_navigator import _is_dropped_from

    substituting = [
        f"{name} 第 {index} 格 配方 {recipe}：{want} → {text}"
        for name, wanted in truth.items()
        for index, want in enumerate(wanted)
        for recipe, text in zip(NAV_VALUE_RECIPES, reads[name][index], strict=True)
        if text and text != want and not (text.isdigit() and _is_dropped_from(text, want))
    ]

    assert len(substituting) == SUBSTITUTING_READS, substituting


def test_the_pool_still_has_recipes_that_never_substitute(reads, truth) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 五套里**至少要有两套**从不替换。

    汇总规则的底线是「一票不通过」：一个值要被采纳，得有两套配方读出同一个东西。
    如果每一套都会替换，那么「两套配方犯同一个臆造」就不再是残余风险，而是常态
    ——`agreed_value` 注释里明写着那是它挡不住的那一种。

    实测这批语料上 `2x/th200/tight` 与 `2x/th170/wide` 两套一次都没替换过。
    """
    from evo_helper.game.system_navigator import _is_dropped_from

    clean = [
        recipe
        for position, recipe in enumerate(NAV_VALUE_RECIPES)
        if not any(
            text and text != want and not (text.isdigit() and _is_dropped_from(text, want))
            for name, wanted in truth.items()
            for index, want in enumerate(wanted)
            for text in [reads[name][index][position]]
        )
    ]

    assert len(clean) >= 2, f"只剩 {clean} 这几套不会替换"


def test_every_adopted_value_has_either_two_recipes_or_a_corroborated_one(  # type: ignore[no-untyped-def]
    reads, counted, truth
) -> None:
    """采纳一个值要么有两套配方背书，要么**一票 + 位数 + 旁证**三样齐全。

    ⚠️ 这里从前断言「必须有 `NAV_VALUE_MIN_VOTES` 票」。2026-08-25 加位数判据时
    那条口径变了，而**变的理由不是放松，是多了一个证人**：位数
    （`digits_on_screen`）不经过 OCR，屏上是 3 位这件事和「某套配方读出 261」
    是两份互不依赖的证据。

    生产上被旧口径挡掉的是 **134 个**格子：

        真值 261 ← ['261', '26', '26', '6', '61']    `261` 只有一票，够票的是截断的 `26`

    ⚠️⚠️ **放宽要带旁证**：其余非空读数必须都能解释成胜出者漏了字（`26`/`6`/`61`
    对 `261` 就是）。少了这一条，`9 ← ['', '3', '', '', '']` 会被采纳成 `3`
    ——第一版就是这样，当场在这批语料上多出一个读错。
    """
    from evo_helper.game.system_navigator import _is_dropped_from

    for name, wanted in truth.items():
        for index in range(len(wanted)):
            got = _adopted(reads, counted, name, index)
            if not got:
                continue
            row = [text for text in reads[name][index] if text.isdigit()]
            votes = sum(1 for text in row if text == got)
            if votes >= NAV_VALUE_MIN_VOTES:
                continue
            assert len(got) == counted[name][index], f"{name} 第 {index} 格：孤证而位数对不上"
            others = [text for text in row if text != got]
            assert others and all(_is_dropped_from(text, got) for text in others), (
                f"{name} 第 {index} 格：孤证 {got!r} 没有旁证，其余读数是 {others}"
            )


def test_the_value_boxes_sit_above_the_label_row_and_never_overlap_it() -> None:
    """值框与标签行分工不同，几何上也必须分开。

    `NAV_LABEL_ROI` 读的是「银河系 / 恒星系 / 行星」这几个字，用来判断在不在恒星系
    视图；值框读的是框里的数。谁把值框往下放宽到标签上，中文就会挤进数字白名单，
    读出来的是噪声——而噪声与空串在这条链路上是两码事：空串安全，噪声会不确认，
    但更糟的是万一噪声恰好是几个数字。
    """
    from evo_helper.game.system_navigator import NAV_LABEL_ROI

    for _left, _top, _right, bottom in NAV_VALUE_ROIS:
        assert bottom <= NAV_LABEL_ROI[1]
