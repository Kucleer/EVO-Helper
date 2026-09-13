"""概览页把攻击与回收拆开之后，几条**漏了不会报错**的判据。

这个文件里的每一条都守着同一种形状的 bug：**改错了跑起来一切正常**，
只是页面上某个数悄悄变成另一个意思。这一夜已经栽过一次同形的
（`_record_dispatch` 的 `mission_kind` 有默认值，漏传就把回收记成攻击，见 `#312`）。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evo_helper.domain.overview import (
    BASIC_SLOTS,
    RARE_SLOTS,
    RECYCLE_STATS_START_UTC,
    SLOT_FLYING,
    SLOT_FLYING_UNKNOWN,
    SLOT_FREE,
    SLOT_RECYCLE,
    SLOT_RECYCLE_UNKNOWN,
    line_slots,
    recovery_rate,
    recycle_rate,
)

TEMPLATES = Path(__file__).resolve().parents[3] / "src" / "evo_helper" / "web" / "templates"


# -- 分母 -----------------------------------------------------------------------


def test_recycles_do_not_dilute_the_report_rate() -> None:
    """⚠️⚠️ **战报回收率的分母是攻击，不是派遣。**

    回收**永远不会产生战报**。实测 2026-09-11：9 发攻击读回 5 份 = 56%。
    把 5 发回收也算进分母就是 5 ÷ 14 = 36% —— 看着像丢了战报，其实一份没丢。
    这正是同一夜「假欠战报」那个 bug 的根子（`pending_reports_for_kind`
    只认 `mission_kind='ATTACK'`）。
    """
    attacks, recycles, reports = 9, 5, 5

    assert recovery_rate(reports, attacks) == pytest.approx(5 / 9)
    # 拿派遣当分母会得到这个假数——钉住它，免得哪天有人"顺手"改回去。
    assert recovery_rate(reports, attacks + recycles) == pytest.approx(5 / 14)
    assert recovery_rate(reports, attacks) > recovery_rate(reports, attacks + recycles)


def test_the_recycle_rate_is_not_clamped_at_one() -> None:
    """⚠️ 回收率可以 >100%，不许截断。

    同一坐标一周能被打两次，而残骸决策是按**航线释放**触发的——
    一轮攻击喂出不止一趟回收是正常的，截成 100% 就把这个真信号抹了。
    """
    assert recycle_rate(12, 10) == pytest.approx(1.2)


def test_both_rates_give_none_instead_of_zero_when_nothing_was_attacked() -> None:
    """一发没打时「0%」是句假话，页面要写「—」。"""
    assert recovery_rate(0, 0) is None
    assert recycle_rate(0, 0) is None
    # ⚠️ 但**打了却一趟没回收**是真的 0，不是 None——这两者页面上长得不一样。
    assert recycle_rate(0, 10) == 0.0


# -- 格子 -----------------------------------------------------------------------


def test_the_four_tiers_keep_the_unknown_ones_next_to_the_free_cells() -> None:
    """次序：攻击 → 回收 → 攻击·钟未知 → 回收·钟未知 → 空。

    两档「钟未知」挨着空格子——这是原来「在飞 → 时长未知 → 空」那条次序的本意
    （一眼看得出「满了，但其中几条是因为读不出飞行时间才占着的」），
    加了类型维度之后照旧成立。
    """
    assert line_slots(
        configured_lines=6,
        holding=4,
        unknown_duration=2,
        recycle_holding=2,
        recycle_unknown=1,
    ) == (
        SLOT_FLYING,
        SLOT_RECYCLE,
        SLOT_FLYING_UNKNOWN,
        SLOT_RECYCLE_UNKNOWN,
        SLOT_FREE,
        SLOT_FREE,
    )


def test_a_recycle_is_never_drawn_as_an_attack() -> None:
    """⚠️ 整条航线都被回收占着时，一格「攻击」都不许画出来。

    漏传回收计数的后果就是这个：页面画满「攻击」，跑起来毫无异常。
    """
    cells = line_slots(
        configured_lines=2, holding=2, unknown_duration=0, recycle_holding=2, recycle_unknown=0
    )

    assert cells == (SLOT_RECYCLE, SLOT_RECYCLE)
    assert SLOT_FLYING not in cells


def test_the_recycle_counts_are_required_keyword_arguments() -> None:
    """⚠️⚠️ **不许给默认值。**

    `_record_dispatch` 的 `mission_kind` 就是栽在默认值上的（`#312`）：
    漏传不报错，只是把回收记成攻击，于是回收喂自己、攻击永远轮不上。
    这里漏传同形——页面把回收画成攻击。宁可当场 `TypeError`。
    """
    with pytest.raises(TypeError):
        line_slots(configured_lines=2, holding=1, unknown_duration=0)  # type: ignore[call-arg]


def test_the_tiers_never_exceed_what_is_actually_held() -> None:
    """回收数比总占用还大（边界上的脏数据）时不许画出多余的格子。"""
    cells = line_slots(
        configured_lines=3, holding=1, unknown_duration=0, recycle_holding=5, recycle_unknown=5
    )

    assert len(cells) == 3
    assert cells.count(SLOT_FLYING) == 0


# -- 「—」与 0 的边界 -------------------------------------------------------------


def test_the_start_line_separates_no_feature_from_a_real_zero() -> None:
    """⚠️⚠️ **上线之后的零必须是 0，不是「—」。**

    写「—」会把「回收链路挂了」显示成「没数据」，正好把故障藏起来——
    同挂机那一列「0 的意思是那段没开机，而我们并不知道」的道理，方向相反：
    那里 0 是假话，这里「—」才是假话。
    """
    day_before = datetime(2026, 9, 9, tzinfo=UTC)
    first_day = RECYCLE_STATS_START_UTC

    # 判据是窗口**右界** ≤ 起点：09-09 那一天的右界正好是 09-10 00:00。
    assert day_before + (first_day - day_before) <= RECYCLE_STATS_START_UTC
    # 上线当天的右界是 09-11，已经越过起点 ⇒ 要显示真实的数（哪怕是 0）。
    assert not (first_day + (first_day - day_before) <= RECYCLE_STATS_START_UTC)


# -- 写死的 colspan --------------------------------------------------------------


#: 表头里那些**会在运行时展开**的 `<th>`：模板里写一个，渲染出 `len(slots)` 个。
#:
#: ⚠️⚠️ **新增一组按槽位展开的列，必须往这里加一行。** 不加的话下面那条用例
#: 会拿一个偏小的列数去比 colspan，于是**真正写对了的 colspan 反而报红**——
#: 一条守着列数的用例自己算错列数，比没有这条用例更难查。
#: 2026-09-13 加基础三样那一列时就是这么发现的：原先的写法把「展开不展开」
#: 做成了一个调用方传进来的布尔量，而那是模板自己的事实，两处必然分家。
SLOT_LOOPS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("rare", RARE_SLOTS),
    ("basics", BASIC_SLOTS),
)


def _column_count(name: str) -> tuple[int, int]:
    """(运行时真实列数, 模板里写死的 colspan)。

    ⚠️ **「这一列展不展开」由模板自己说了算，不由调用方传。** 两张表长得不一样：
    周期统计那张的稀有列是 `{% for cell in ... .rare %}`，一个 `<th>` 展开成
    `len(RARE_SLOTS)` 列；星球效率那张的稀有只有**一列合计**，不展开；
    而 2026-09-13 之后两张表都有一个展开的 `basics` 循环。所以这里直接从
    `<thead>` 的源码上认循环，认出一个就把那一个 `<th>` 记成 `len(slots)` 列。
    """
    html = (TEMPLATES / name).read_text(encoding="utf-8")
    # ⚠️ 只看 `<thead>`：表体里同名的循环是**格子**不是列，数进来会翻倍。
    head = html.split("</thead>")[0]
    columns = len(re.findall(r'<th scope="col"', head))
    for attr, slots in SLOT_LOOPS:
        if re.search(rf"{{%\s*for cell in \([^)]*\.{attr}\b", head):
            columns += len(slots) - 1
    spans = {int(value) for value in re.findall(r'colspan="(\d+)"', html)}
    assert len(spans) == 1, f"{name} 里有不止一个 colspan：{spans}"
    return columns, spans.pop()


@pytest.mark.parametrize("template", ["_overview_periods.html", "_overview_origins.html"])
def test_the_hardcoded_colspan_matches_the_real_column_count(template: str) -> None:
    """⚠️ 两张表的 `colspan` 都是**写死的**，加减列很容易漏改。

    漏改不会报错，只会让「这一档没有数据」那一行的底色少铺或多铺几列——
    而那正是数据为空时唯一会显示的东西。
    """
    columns, colspan = _column_count(template)

    assert colspan == columns, f"{template}: 实际 {columns} 列，colspan 写的是 {colspan}"


@pytest.mark.parametrize("template", ["_overview_periods.html", "_overview_origins.html"])
def test_both_tables_show_the_basic_three(template: str) -> None:
    """⚠️ 用户口径 2026-09-13（原话在 `domain.overview.BASIC_SLOTS` 上）：
    **两张表**都要有基础三样，攻击与回收都算进去。

    只改一张是这次最容易犯的错——两张表的列几乎一一对应，改完一张很像已经做完了。
    """
    head = (TEMPLATES / template).read_text(encoding="utf-8").split("</thead>")[0]

    assert re.search(r"{%\s*for cell in \([^)]*\.basics\b", head), (
        f"{template} 的表头里没有基础三样那一组列"
    )


# -- 模板里那个分支写法 -----------------------------------------------------------


@pytest.mark.parametrize("template", ["_overview_periods.html", "_overview_origins.html"])
def test_the_dash_branches_on_the_start_line_not_on_the_count(template: str) -> None:
    """⚠️⚠️ **「—」必须判 `recycle_before_start`，不许判「回收数是不是 0」。**

    写成 `{% if not row.recycles %}—{% endif %}` 渲染出来一模一样
    （上线前那些天回收数确实是 0），**用例也照样全绿** ——
    直到某天回收链路挂了，那一天的 0 被显示成「没数据」，
    而那正是最需要它喊出来的时候。

    这是「用例守着一个 bug」的同一种形状：两种写法在**现有数据上**
    行为完全一致，只在故障时分叉。所以只能从源码上钉。
    """
    html = (TEMPLATES / template).read_text(encoding="utf-8")

    assert "row.recycle_before_start" in html, f"{template} 没有按上线起点判「—」"
    # 渲染回收数那两格里，不许出现「数为 0 就写 —」这种写法。
    assert "if not row.recycles" not in html
    assert "if row.recycles %}{{ row.recycles }}{% else %}—" not in html


# -- slot code 三处要对齐 ---------------------------------------------------------


def test_every_slot_code_has_a_label_and_a_style() -> None:
    """⚠️ 每个 slot code 要在**三处**都齐：常量、模板的字面量 map、CSS。

    - 模板漏了 ⇒ Jinja 当场 `KeyError`（这是它比 `|default` 好的地方）。
    - **CSS 漏了 ⇒ 一声不响**：格子照画，只是没有底色没有边框，
      在深色页面上看起来就是一块空白——而这正是「回收」这一档最可能出的事，
      因为它是唯一一个新加的颜色。

    所以这一条只为 CSS 那一侧存在。
    """
    codes = {
        SLOT_FLYING,
        SLOT_FLYING_UNKNOWN,
        SLOT_RECYCLE,
        SLOT_RECYCLE_UNKNOWN,
        SLOT_FREE,
    }
    now_html = (TEMPLATES / "_overview_now.html").read_text(encoding="utf-8")
    css = (TEMPLATES.parent / "static" / "console.css").read_text(encoding="utf-8")

    for code in sorted(codes):
        assert f"'{code}':" in now_html, f"模板的文案 map 里没有 {code!r}"
        # ⚠️ **要词边界，不能用子串。** 第一版写的是 `f".slot-{code}" in css`，
        # 变异验证当场打脸：把 `.slot-recunk` 改名成 `.slot-recunkX` 之后
        # 用例照样全绿（`.slot-recunkX` 里含着 `.slot-recunk`）。
        # 一条守不住东西的用例比没有更糟——它会让人以为这里有防线。
        assert re.search(r"\.slot-" + code + r"(?![0-9A-Za-z_-])", css), (
            f"console.css 里没有 .slot-{code}——格子会画成一块空白"
        )


def test_the_recycle_colour_is_not_one_of_the_status_colours() -> None:
    """⚠️ 回收**不是一种状态**，不许复用 ok / warn / danger。

    用 `--ok` 会把「这条航线在回收」读成「一切正常」，用 `--warn` 会读成
    「要留意」。同 `--pirate`（那是「这是个海盗」）和 `.tone-rare`
    （那是「稀有资源」）分出来的理由。
    """
    css = (TEMPLATES.parent / "static" / "console.css").read_text(encoding="utf-8")
    recycle = re.search(r"--recycle:\s*(#[0-9a-fA-F]{6})", css)
    assert recycle is not None, "console.css 里没有 --recycle"

    taken = dict(re.findall(r"--(ok|warn|danger|accent|pirate):\s*(#[0-9a-fA-F]{6})", css))
    assert recycle.group(1).lower() not in {value.lower() for value in taken.values()}, (
        f"回收用了一个已经有别的含义的颜色：{taken}"
    )


def test_the_recycle_rate_over_one_hundred_is_explained_not_clamped() -> None:
    """⚠️ >100% 是**跨日错位**，不是超量回收。

    一发攻击和它喂出的回收之间隔着半小时到两小时（航线先释放、决策才触发），
    跨过 UTC 零点（= 本地 08:00）的那一发**攻击记昨天、回收记今天**。

    实测 2026-09-11：`9:250:8` 攻击 4 / 回收 5 = 125%，而那第 5 趟的源头攻击
    在本地 07:43——算昨天。同一天全局是 29 : 29 = 100%，一趟没多。

    这一条钉两件事：**不许截断**，以及**页面上得有话解释**——
    不写清楚的话下一个人会去查一个不存在的「重复派遣」bug（我就查过）。
    """
    assert recycle_rate(5, 4) == pytest.approx(1.25)

    for name in ("_overview_periods.html", "_overview_origins.html"):
        tip = (TEMPLATES / name).read_text(encoding="utf-8")
        assert "超过 100% 不是超量回收" in tip, f"{name} 没有解释 >100% 是怎么来的"
