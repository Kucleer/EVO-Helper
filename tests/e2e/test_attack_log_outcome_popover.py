"""攻击日志「战果」那一列：展开走浮层，**不许把行撑高**。

用户口径 2026-08-19：「这里的效果我希望是像 tips 那种 hover 效果，而不是这种
增加列高的」。此前这一格跟系统日志页共用 `tr:hover .log-line`——鼠标停上去
换行展开，战损加 12 项收获把那一行撑到十几行高、占掉半个屏幕，下面的行整片
往下跳，想点的那一行已经不在鼠标底下了。

现在收起态固定「chip 一行 + 摘要一行」，全文放在一块绝对定位的浮层里。

⚠️ **共用类只共用了限宽那一半。** `.log-body` 两页仍然共用；`.log-line` 那套
`tr:hover` 展开**原样留给系统日志**，那一页的「正文」列里是 `payload_json` 加
一张 base64 缩略图，缩略图本来就靠 hover 放大（`.log-shot`），塞进一块定宽浮层
反而把最该看的那张图变小了。症状看着一样，要的东西不一样。

没有浏览器，量不了真实的行高，所以这几条钉的是**能在 HTML 与 CSS 上判定的那些
判据**：浮层是不是触发元素的后代（决定那个链接点不点得着）、12 项资源是不是
还在 DOM 里（决定折叠有没有偷偷删数据）、有没有哪条规则在 hover 时改尺寸
（决定行高稳不稳）、键盘焦点能不能把它打开。**浏览器里的实际观感这几条管不到。**
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
    BattleResourceEntry,
)
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.report_screenshots import ReportScreenshotRepository
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.pirate_reports import OUTCOME_VICTORY
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.database import scratch_database_url
from support.runs import seed_run_instance

ORIGIN = Coordinate(2, 137, 18)
TARGET = Coordinate(2, 137, 4)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
DISPATCHED = datetime(2026, 8, 17, 3, 55, tzinfo=UTC)
PRESET = FleetPresetRef(name="海盗清扫-主力", signature="深空吞噬者:70")

#: 12 格全部非零——正是把这一页撑破的那种记录。前四项是画面上缩写显示的近似值。
FULL_HAUL = tuple(
    BattleResourceEntry(slot=slot, amount=amount, approximate=approximate, uncertainty=uncertainty)
    for slot, amount, approximate, uncertainty in (
        (0, 774_600, True, 50),
        (1, 553_400, True, 50),
        (2, 72_000, True, 500),
        (3, 3_400, True, 50),
        (4, 657, False, 0),
        (5, 183, False, 0),
        (6, 16, False, 0),
        (7, 33, False, 0),
        (8, 12, False, 0),
        (9, 4, False, 0),
        (10, 21, False, 0),
        (11, 9, False, 0),
    )
)

#: 那 12 格确认过的资源名（`domain.battle_resources.SLOT_LABELS` 的顺序）配上
#: 各自的数量。**逐项写死在这里**：只断言「有 12 个 span」的话，把数字换成别的
#: 也照样绿，而这一列出错最安静的方式恰恰就是数字对不上名字。
EXPECTED_HAUL = (
    "金属 约 774,600",
    "晶体 约 553,400",
    "气体 约 72,000",
    "暗能量 约 3,400",
    "银河素 657",
    "合金碎片 183",
    "晶体矿石 16",
    "能量凝胶 33",
    "泰坦立方 12",
    "收割者碎片 4",
    "银河石碎片 21",
    "银河石能量 9",
)

#: 「战果」是表头里的第 9 列（1 起数）。同 `test_attack_log_width.py`，跟着列序走。
OUTCOME_COLUMN = 9


def _client(
    tmp_path: Path,
    *,
    resources: tuple[BattleResourceEntry, ...] = FULL_HAUL,
    with_screenshot: bool = True,
) -> TestClient:
    engine = create_database_engine(scratch_database_url(tmp_path, "outcome-popover.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: DISPATCHED)
    plan = service.create_plan(
        name="海盗攻击",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        ranges=(
            ScanRangeView(Coordinate(2, 137, 1), TARGET, ORIGIN, PRESET.name, PRESET.signature, 0),
        ),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="popover-0001", created_at_utc=DISPATCHED
    )
    repository = SqlAlchemyRepository(factory)
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=TARGET,
        preset=PRESET,
        cycle_start_utc=CYCLE,
        created_at_utc=DISPATCHED - timedelta(minutes=1),
        target_kind=TARGET_KIND_PIRATE,
    )
    repository.save_attack_intent(intent)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=DISPATCHED,
            accepted=True,
        )
    )
    report_id = uuid4()
    repository.append_report(
        BattleReport(
            report_id=report_id,
            reported_at_utc=DISPATCHED + timedelta(minutes=43),
            attacker_origin=ORIGIN,
            defender_target=TARGET,
            raw_time_text="17/08/2026 04:38:46",
            outcome=OUTCOME_VICTORY,
            attacker_losses=0,
            defender_losses=1120,
            resources=resources,
        )
    )
    if with_screenshot:
        # 字节内容无关紧要，这几条要的只是「这一行有图」这个事实。
        # ⚠️ 仓库是公开的，**别往里放真的图片文件**。
        ReportScreenshotRepository(factory).save(
            report_id,
            image_bytes=b"not-a-real-image",
            width=520,
            height=695,
            captured_at_utc=DISPATCHED + timedelta(minutes=43),
        )
    return TestClient(create_persistent_app(factory))


def _outcome_cell(html: str) -> str:
    """把表格体第一行里「战果」那一格取出来，连 `<td` 一起（下面要解析它）。"""
    start = html.find("<tbody")
    assert start != -1, "页面上没有表格体，这几条用例的前提就不成立"
    row = re.search(r"<tr[^>]*>(.*?)</tr>", html[start:], re.DOTALL)
    assert row is not None, "表格体里一行都没有，这几条用例的前提就不成立"
    cells = row.group(1).split("<td")
    assert len(cells) > OUTCOME_COLUMN, f"这一行只有 {len(cells) - 1} 格，取不到「战果」那一列"
    return "<td" + cells[OUTCOME_COLUMN]


class _Element:
    """解析出来的一个标签，连同它头上那串祖先的 class。"""

    def __init__(self, tag: str, attrs: dict[str, str | None], ancestors: tuple[str, ...]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.classes = tuple((attrs.get("class") or "").split())
        #: 从外到内，每一层祖先身上的 class 全都摊平在这里。
        self.ancestors = ancestors
        #: 这个标签范围内的文字（含后代的）。
        self.text = ""


class _Tree(HTMLParser):
    """把一小段 HTML 解析成「谁在谁里面」。

    ⚠️ **不能拿字符串包含关系代替嵌套关系。** 这几条用例里最要紧的一条就是
    「浮层是不是触发元素的**后代**」——把浮层挪成触发元素的兄弟，两者仍然在
    同一格里、任何 `in` 断言都照样绿，而实机上那个截图链接已经点不着了。
    """

    def __init__(self, fragment: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[_Element] = []
        self._stack: list[_Element] = []
        self.feed(fragment)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        here = tuple(name for frame in self._stack for name in frame.classes)
        element = _Element(tag, dict(attrs), here)
        self.elements.append(element)
        self._stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        if self._stack:
            self._stack.pop()

    def handle_data(self, data: str) -> None:
        # 文字算进当前**所有**还没闭合的标签：判「这一项写没写『约』」时要的是
        # 那一项自己的文字，而判「这一格里有没有某句话」时要的是外层的。
        for frame in self._stack:
            frame.text += data

    def one(self, class_name: str) -> _Element:
        found = [item for item in self.elements if class_name in item.classes]
        assert len(found) == 1, f"这一格里 `.{class_name}` 出现了 {len(found)} 次，期望正好一次"
        return found[0]

    def all(self, class_name: str) -> list[_Element]:
        return [item for item in self.elements if class_name in item.classes]


# -- 一、浮层必须留在触发元素的作用域里，否则那个链接点不着 ----------------------


def test_the_popover_is_a_descendant_of_the_thing_that_opens_it(tmp_path: Path) -> None:
    """浮层是触发元素的**后代**，不是它的兄弟。

    ⚠️ 这一条钉的是「查看战报截图」还点不点得着。纯 CSS 的 hover 浮层若挂在触发
    元素之外，鼠标从摘要走向面板的路上 `:hover` 就断了，面板消失——那个链接
    永远够不到。CSS 那两条（`.outcome-detail:hover > .outcome-pop` 与
    `:focus-within`）都建立在这层嵌套上，嵌套一破，两条一起失效。

    按解析出来的树判，不按字符串包含判：挪成兄弟节点的话，两者仍然在同一格里。
    """
    tree = _Tree(_outcome_cell(_client(tmp_path).get("/logs").text))

    popover = tree.one("outcome-pop")
    assert "outcome-detail" in popover.ancestors, (
        "浮层跑到触发元素外面去了——鼠标走过去的路上 hover 会断，截图链接点不着"
    )


def test_the_screenshot_link_lives_inside_the_popover(tmp_path: Path) -> None:
    """截图链接在浮层里，而且它是个真链接（有 href，鼠标走得进去）。

    收起态里没有它——放在摘要那一行就又要占宽度，而这一列的宽度是 2026-08-18
    那条横向滚动条的来源。
    """
    tree = _Tree(_outcome_cell(_client(tmp_path).get("/logs").text))

    links = [item for item in tree.elements if item.tag == "a"]
    assert len(links) == 1, f"这一格里有 {len(links)} 个链接，期望正好一个（战报截图）"
    link = links[0]
    href = link.attrs.get("href") or ""
    assert re.fullmatch(r"/api/reports/[0-9a-f-]{36}/screenshot", href), (
        f"截图链接的地址成了 {href!r}"
    )
    UUID(href.split("/")[3])
    assert "outcome-pop" in link.ancestors, "截图链接不在浮层里了"
    assert "outcome-detail" in link.ancestors


def test_the_stylesheet_keeps_the_popover_open_from_the_trigger(tmp_path: Path) -> None:
    """样式表这一侧：撑住浮层的是**触发元素**的 `:hover` / `:focus-within`。

    改成 `tr:hover` 或者 `td:hover` 看着也能用，但那样鼠标一旦离开这一行（比如
    面板向上翻、鼠标要往上走）面板就没了；改成 `.outcome-pop:hover` 更糟——
    面板得先显示出来才可能被 hover，永远打不开。
    """
    css = _console_css()

    assert ".outcome-detail:hover > .outcome-pop," in css
    assert ".outcome-detail:focus-within > .outcome-pop {" in css


# -- 二、键盘也能打开 ------------------------------------------------------------


def test_the_trigger_is_reachable_by_keyboard(tmp_path: Path) -> None:
    """触发元素挂着 `tabindex="0"`，照 `.tips` 那套（`missions.html` 里那几个 ⓘ）。

    ⚠️ 只能 hover 触发的信息，键盘用户拿不到。而这不是可有可无的附注——面板里
    装着这一发的全部收获和战报截图链接，够不着它等于这一列对键盘用户不存在。
    """
    tree = _Tree(_outcome_cell(_client(tmp_path).get("/logs").text))

    assert tree.one("outcome-detail").attrs.get("tabindex") == "0", (
        "触发元素没挂 `tabindex`，键盘 Tab 不到它，浮层就永远打不开"
    )


def test_focus_alone_opens_the_popover(tmp_path: Path) -> None:
    """CSS 用的是 `:focus-within` 而不是 `:focus`。

    差别在焦点走进面板之后：`:focus`（只认触发元素自己）会在 Tab 落到里面那个
    截图链接的一瞬间把面板收起来，链接连同焦点一起消失——键盘上的表现，正是
    鼠标那条「浮层脱离触发区」的翻版。

    收起态也不能用 `display: none`：那里面的链接不可聚焦，`:focus-within` 就
    没有机会成立（整段理由在 console.css 那一条上）。
    """
    css = _console_css()
    block = _rule_block(css, ".outcome-pop {")

    assert "visibility: hidden" in block, "收起态改成了别的方式，`:focus-within` 那条链子会断"
    assert "display: none" not in block
    assert ".outcome-detail:focus-visible" in css, "键盘落上去没有焦点框，看不出焦点在哪"


# -- 三、12 项资源是被藏起来，不是被删掉 -----------------------------------------


def test_every_resource_line_is_still_in_the_dom(tmp_path: Path) -> None:
    """12 项收获一项不少地留在浮层里。

    ⚠️ **折叠只许发生在 CSS 里。** 改成浮层之后最省事的做法是「摘要里只写前三
    项」，页面看着一样清爽，而用户查故障时要的正是后面那几项——它们才是稀有
    材料。战损两个数同理。
    """
    cell = _outcome_cell(_client(tmp_path).get("/logs").text)
    tree = _Tree(cell)

    items = tree.all("outcome-haul-item")
    assert len(items) == 12, f"浮层里只剩 {len(items)} 项收获，折叠不该删数据"
    for item in items:
        assert "outcome-pop" in item.ancestors, "收获跑到浮层外面去了，那一行又要被撑宽"
    for text in EXPECTED_HAUL:
        assert text in cell, f"浮层里少了 {text}——折叠不该删数据"
    assert "战损 我 0 · 敌 1120" in cell


def test_the_precision_marks_survive_the_move_into_the_popover(tmp_path: Path) -> None:
    """近似值上的「约」与 `title` 里的误差范围跟着一起搬进浮层。

    ⚠️ 把 ±500 的估算显示成确数，比不显示更糟（仓库里已有先例：
    `military_score_estimated`）。改版最容易顺手带走的就是这两样——它们
    一个在文字里、一个在属性里，重排 DOM 时都不显眼。
    """
    tree = _Tree(_outcome_cell(_client(tmp_path).get("/logs").text))

    hints = [item.attrs.get("title") or "" for item in tree.all("outcome-haul-item")]
    assert "画面上是缩写显示的，误差不超过 ±500" in hints, "误差范围那个 title 丢了"
    assert "精确读数" in hints, "精确读到的那几项也该说清楚自己是精确的"
    approximate = [item for item in tree.all("outcome-haul-item") if "约" in item.text]
    assert len(approximate) == 4, "带「约」的项数不对——近似值被当成确数显示了"
    # 「约」和 title 必须是同一批：文字上说「约」而 title 说「精确读数」（或者
    # 反过来）的话，页面自己在打自己的脸。
    for item in approximate:
        assert "缩写" in (item.attrs.get("title") or ""), (
            f"{item.text!r} 标了「约」，title 却说它是确数"
        )


# -- 四、hover 不许再改变行高 -----------------------------------------------------


def _console_css_raw() -> str:
    return (
        Path(__file__).resolve().parents[2]
        / "src"
        / "evo_helper"
        / "web"
        / "static"
        / "console.css"
    ).read_text(encoding="utf-8")


def _console_css() -> str:
    """样式表，**注释先剥掉**。

    ⚠️ 这个仓库里注释比代码长，而注释里成段引用着规则本身（`display: none`、
    `tr:hover .log-line` 都被引用了不止一处）。不剥的话两头都会说假话：
    「这条规则还在」会被一句谈论它的注释喂饱，「这条规则里不许出现 X」会被一句
    解释 X 为什么不能用的注释判红。
    """
    return re.sub(r"/\*.*?\*/", "", _console_css_raw(), flags=re.DOTALL)


def _rule_block(css: str, selector: str) -> str:
    """把某条规则的声明块取出来（含选择器那一行）。"""
    start = css.find(selector)
    assert start != -1, f"console.css 里找不到 `{selector}`"
    end = css.find("}", start)
    assert end != -1, f"`{selector}` 那条规则没有收尾的大括号"
    return css[start:end]


def test_the_popover_is_out_of_flow(tmp_path: Path) -> None:
    """浮层绝对定位——**它不占版面，所以它有多高都不改变行高**。

    这一条钉的就是用户 2026-08-19 说的那件事。把 `position: absolute` 换成
    `static`（或者干脆让它跟着 `display` 走），12 项收获立刻回到文档流里，
    那一行原样被撑到十几行高，整张表跟着跳。
    """
    block = _rule_block(_console_css(), ".outcome-pop {")

    assert "position: absolute" in block, "浮层回到了文档流里，hover 又会把这一行撑高"


def test_the_collapsed_summary_never_grows(tmp_path: Path) -> None:
    """收起态那一行摘要永远是一行：`nowrap` + 省略号，且没有任何 hover 覆盖。

    此前撑高的元凶是 `tr:hover .log-line { white-space: pre-wrap }`——一条把
    「单行省略」在 hover 时翻掉的规则。这一条盯着同一个形状别再出现：撑住浮层
    的那条规则里只许出现「显不显示」，不许出现任何改尺寸的声明。
    """
    css = _console_css()
    peek = _rule_block(css, ".outcome-peek {")

    assert "white-space: nowrap" in peek
    assert "text-overflow: ellipsis" in peek

    reveal = _rule_block(css, ".outcome-detail:hover > .outcome-pop,")
    declarations = {
        line.split(":")[0].strip()
        for line in reveal.split("{")[1].split(";")
        if ":" in line and not line.strip().startswith("/*")
    }
    assert declarations == {"visibility", "opacity", "pointer-events"}, (
        f"展开那条规则里多了改尺寸的声明：{sorted(declarations)}——行高又会跟着 hover 变"
    )


def test_the_outcome_column_no_longer_uses_the_row_hover_expansion(tmp_path: Path) -> None:
    """这一格里不许再出现 `.log-line`。

    那个类带着 `tr:hover .log-line { white-space: pre-wrap }`，正是把一行撑到
    半屏的那条规则。它本身还留在样式表里给系统日志用（那一页的判据在
    `test_attack_log_width.py`），所以只能从这一格的 HTML 上挡。
    """
    cell = _outcome_cell(_client(tmp_path).get("/logs").text)

    assert "log-line" not in cell


# -- 五、没东西可展开时不摆一个空浮层 ---------------------------------------------


def test_a_row_with_nothing_to_expand_gets_no_trigger(tmp_path: Path) -> None:
    """既没收获也没截图的那一行：摘要照写，但不做成能展开的东西。

    ⚠️ 摆一个点上去什么都没有的「ⓘ」，比没有它更难判断「到底是没捞着还是
    没记」——同「目标军力」那一格里角标的理由（`logs.html` 上的注释）。
    """
    cell = _outcome_cell(_client(tmp_path, resources=(), with_screenshot=False).get("/logs").text)

    assert "战损 我 0 · 敌 1120" in cell, "战损两个数不该跟着浮层一起消失"
    assert "outcome-detail" not in cell, "没东西可展开的行也挂上了触发元素"
    assert "outcome-pop" not in cell
