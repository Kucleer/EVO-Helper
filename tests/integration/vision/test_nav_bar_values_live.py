"""导航栏三个**值框**的 ROI、配方与汇总规则，跑在 43 张真实截图上。

## ⚠️ 为什么语料和真值都不在仓库里

本仓是**公开仓库**，而截图与坐标都是能反推账号的东西（`.gitignore` 第二段写着
2026-08-18 那次把 34 份战报面板当夹具提交、只能整个撤回的事）。所以这里照
`test_resource_grid_corpus_live.py` 的规矩办：图和真值都放 `var/` 下，本机有才跑，
CI 里跳过；**committed 的这份只断言统计量**，不写任何一个具体坐标。

## 语料长什么样、怎么备齐

- 图：`var/logs/*.png`，1920×917 的 client 空间整帧，导航栏标签行读得出
  「银河系 / 恒星系 / 行星」两个以上（`on_system_view`）的那些。
- 真值：`var/fixtures/vision/nav_bar_values.json`，`{文件名: [银河系, 恒星系, 行星]}`，
  **逐张目视放大核对过**。它是这套识别唯一的裁判。

## 这里守的是什么

**上一版的结论在实机上是错的，这份语料就是为了不再错第二次。** 老注释说选中的
三套配方「只会读空，不会读错」，依据是九张实拍上「八张全对、零张读错」——可那
九张里八张的恒星系框都是 `137`，而 `137` 是这套字体里最结实的数，每套配方都读得对。
语料里压根没有「首位是 2 的多位数」，于是那个结论从来没被考验过。生产
`system_log` 2026-08-18 给出了反例：**28 次回读、28 次对不上**，`277` 读成 `77`、
`250` 读成 `50`。同一种错法在这 43 张里也复现得到（`27`→`7`、`52`→`5`）。

所以判据换成两条，**缺一不可**：

1. `PERFECT_SHOTS` / `MISREAD_CELLS` —— 汇总规则跑下来的成绩，读错必须是 0。
2. `test_every_recipe_in_the_pool_only_ever_drops_digits` —— **池子里每一套配方
   单独看，在这 43 张上错法只能是漏字**。含替换错法的配方大量进池会把汇总规则
   带崩（实测把 18 套候选全塞进去，读错格从 0 涨到 9）。

## ⚠️⚠️ 这份语料只有成功样本，**它证明不了实机上不出错**

2026-08-25 又栽了一次，还是同一个坑。`agreed_value` 当时的最后一条判据
（「其余非空读数都必须能解释成漏字」）明写着安全性架在上面第 2 条那条性质上。
生产读数把那条性质否掉了：`15` 被读成 `6`、`391` 被读成 `3931`、`117` 被读成 `7`
——替换和凭空多位都有。**而这 43 张里恰好没有 `15`／`6`／`117`／`261`／`391`／`9`
这几个字形**，所以第 2 条一直是绿的。

后果不是「读得差一点」：那条判据反过来否决了正确读数，生产 1290 个值框里
丢掉 123 个。判据已经窄化，不再依赖那条性质（账在 `agreed_value` 的注释里）。

⇒ **这份语料回答的是「本来就读得对的格子会不会被弄坏」，不是「实机上会不会出错」。**
另一半在 `tools.nav_readback_replay`：它拿几百条**生产失败读数**给候选规则打分。
两边都要过。改配方或改汇总规则时**只跑一边，就是把这个坑再踩一次**。
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

#: 语料份数（43 张 × 3 个值框 = 129 个格子），2026-08-18 那一批，全部人工核过。
CORPUS_SIZE = 43

#: 三个格子全读对的份数。
#:
#: ⚠️ **这个数是判据，两个方向都不许悄悄变。** 涨上去说明识别变准了（好事，改大），
#: 掉下来说明有东西回归了。老配方 + 老规则（「首个非空」）在同一批语料上也是 35，
#: 但那 35 是**带着 3 个读错格**换来的——见下面 `MISREAD_CELLS`。
PERFECT_SHOTS = 36

#: 汇不拢、交空串的格子数（129 格里 7 格）。空串走「读不通就不确认」那一支，
#: 代价只是下一个目标白设两个字段。**这是承认，不是豁免**：剩下的 7 格是
#: 6 个「行星框写着两位数、每套配方都只读出一位」加 1 个「单个数字一套都读不出」。
UNREADABLE_CELLS = 7

#: 读错的格子数。**必须是 0，而且永远只能是 0。**
#:
#: ⚠️ 这一条是整份文件的承重墙。读空只是白设两个字段，读错要付的是缓存与导航栏
#: 分岔——`SystemNavigator` 类注释里那次 136→9，连续 44 个目标核对全不过、13 分钟
#: 一发没派。老配方 + 老规则在这批语料上是 3。
MISREAD_CELLS = 0

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


def test_every_recipe_in_the_pool_only_ever_drops_digits(reads, truth) -> None:  # type: ignore[no-untyped-def]
    """池子里每一套配方**单独看**，在这 43 张上错法只能是「漏掉了某几位」。

    这是**选配方的判据**：被剔掉的 `(3,140)` / `(4,140)` 就是栽在这里，
    实拍上它们把 `9` 读成 `93`（凭空多一位）。留着它是为了让下一个往池子里加配方
    的人先过这一关。

    ## ⚠️⚠️ 但它**不是** `agreed_value` 的安全性依据 —— 这句话曾经写在这里，是错的

    2026-08-25 之前这段注释写着「`agreed_value` 的安全性整个架在这条性质上」，
    而那条汇总规则的最后一条判据（「其余非空读数都必须能解释成漏字」）确实照着
    这句话写的。**生产读数否掉了它**：`15`→`6`、`391`→`3931`、`117`→`7`，
    替换和多位都有。

    这条用例当时是绿的，因为**这 43 张里恰好没有那几个字形**。绿灯来自语料的盲区，
    不是来自性质成立。判据已经窄化成「只否决『有配方看见了更多位』」，
    不再依赖这条性质。

    ⇒ 这条用例**红了要重挑配方，绿了什么都不能推论**。实机会不会出错，
    去看 `tools.nav_readback_replay`。
    """
    from evo_helper.game.system_navigator import _is_dropped_from

    bad = []
    for name, wanted in truth.items():
        for index, want in enumerate(wanted):
            for recipe, text in zip(NAV_VALUE_RECIPES, reads[name][index], strict=True):
                if not text or text == want:
                    continue
                if not (text.isdigit() and _is_dropped_from(text, want)):
                    bad.append(f"{name} 第 {index} 格 配方 {recipe}：错法不是漏字")
    assert bad == [], bad


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
