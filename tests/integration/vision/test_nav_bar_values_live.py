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
PERFECT_SHOTS = 4

#: 汇不拢、交空串的格子数（27 格里 6 格）。空串走「读不通就不确认」那一支，
#: 代价只是下一个目标白设两个字段。**这是承认，不是豁免**，六格各自的成因：
#:
#:     9   ← ['', '3', '', '', '']                 只有一票，还是替换
#:     11  ← ['', '1', '', '', '']    （两张）      两位数只读出一位，一票
#:     12  ← ['', '12', '2', '', '']               对的那个只有一票
#:     297 ← ['297','237','27','37','97']          五套各说各话，没有一个够票
#:     189 ← ['189','189','189','1893','189']      ⚠️ 四票的 `189` 被那个 `1893` 否掉
#:
#: 最后一条值得单说：**一套配方凭空多读一位，就能否掉四票一致的正确读数。**
#: 老判据同样交空（`1893` 解释不成 `189` 漏字），所以不是这次窄化造成的；
#: 但它和生产上 `391 ← [...,'3931',...]` 是同一个形状，说明「尾部多一位」是这套
#: 配方的一种**反复出现**的错法，重挑配方时该拿它当靶子。
UNREADABLE_CELLS = 6

#: 读错的格子数。**必须是 0，而且永远只能是 0。**
#:
#: ⚠️ 这一条是整份文件的承重墙。读空只是白设两个字段，读错要付的是缓存与导航栏
#: 分岔——`SystemNavigator` 类注释里那次 136→9，连续 44 个目标核对全不过、13 分钟
#: 一发没派。
MISREAD_CELLS = 0

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


def test_the_corpus_is_the_size_this_file_talks_about(truth) -> None:  # type: ignore[no-untyped-def]
    """底下几个数都是按这批语料量的；语料换了，那几个数就得重量。"""
    assert len(truth) == CORPUS_SIZE


def test_the_pooled_reading_never_gets_a_number_wrong(reads, truth) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **一个格子都不许读错。** 读空可以，读错不行。

    这正是老版本失守的地方：老注释断言「只会读空不会读错」，实机上却把
    `277` 读成 `77`。这条用真语料把那句话钉住。
    """
    wrong = []
    for name, wanted in truth.items():
        for index, want in enumerate(wanted):
            got = agreed_value(reads[name][index])
            if got and got != want:
                # 报错里只说形状，不抄坐标——本仓是公开仓库。
                wrong.append(f"{name} 第 {index} 格：读出 {len(got)} 位，真值 {len(want)} 位")
    assert len(wrong) == MISREAD_CELLS, wrong


def test_the_pooled_reading_gets_this_many_shots_completely_right(reads, truth) -> None:  # type: ignore[no-untyped-def]
    """三格全对的份数。掉下来就是回归，涨上去把常量改大。"""
    perfect = sum(
        1
        for name, wanted in truth.items()
        if tuple(agreed_value(row) for row in reads[name]) == tuple(wanted)
    )
    assert perfect == PERFECT_SHOTS


def test_the_cells_that_stay_unreadable_are_counted_not_hidden(reads, truth) -> None:  # type: ignore[no-untyped-def]
    """读不出来的格子有几个，明写出来。**这是承认，不是豁免。**"""
    blank = sum(
        1
        for name, wanted in truth.items()
        for index in range(len(wanted))
        if not agreed_value(reads[name][index])
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


def test_at_least_two_recipes_back_every_value_that_gets_adopted(reads, truth) -> None:  # type: ignore[no-untyped-def]
    """采纳的值必须真的有 `NAV_VALUE_MIN_VOTES` 套配方读出来过。

    守的是「一票不通过」这条规矩在真语料上确实兑现了——老规则的「首个非空」
    等价于一票通过，那就是这次修的缺陷本体。
    """
    for name, wanted in truth.items():
        for index in range(len(wanted)):
            got = agreed_value(reads[name][index])
            if not got:
                continue
            votes = sum(1 for text in reads[name][index] if text == got)
            assert votes >= NAV_VALUE_MIN_VOTES, f"{name} 第 {index} 格只有 {votes} 票"


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
