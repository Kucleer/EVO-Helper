"""攻击日志「战果」那一列：摘要摆核心三样，详情点开是弹窗，**行高一律不变**。

这一批用例接着 PR #214 往下走，那一版把这一列从「就地展开撑高行」改成了 hover
浮层。两条口径都来自用户 2026-08-19：

* 「收货 12 项，改成核心 3 资源数量」——摘要不再只说「有没有」，而是把
  **合金碎片 / 泰坦立方 / 收割者碎片**（`domain.overview.RARE_SLOTS`）的数量
  摆出来；其余九项一项不少地留在弹窗里。
* 「做一个弹窗然后关闭会效果很好么，保持列的高度不变」——hover 浮层换成原生
  `<dialog>`。**光换形状是净亏**（多两次点击），真正值钱的是**战报截图从此内嵌
  在弹窗里**：以前它是个链接、点了跳新标签页，「看数字」和「看原图」是两个动作。

⚠️ **共用类只共用了限宽那一半。** `.log-body` 两页仍然共用；`.log-line` 那套
`tr:hover` 展开**原样留给系统日志**，那一页的「正文」列里是 `payload_json` 加
一张 base64 缩略图，缩略图本来就靠 hover 放大（`.log-shot`）——挪进要点一下才
出来的弹窗，最该扫一眼的那张图反而多了一次点击。症状看着一样，要的东西不一样。

没有浏览器，量不了真实的行高、也点不开真的弹窗，所以这几条钉的是**能在 HTML /
CSS / 那段脚本上判定的那些判据**：详情是不是待在 `<template>` 里（决定行高稳不
稳）、12 项资源是不是还在（决定折叠有没有偷偷删数据）、摘要里那三样在不在、
截图是不是只有 `data-src`（决定列表里加不加载它）、弹窗在不在被自动刷新整块换掉
的那一块之外（决定刷新会不会把开着的弹窗掀掉）。**浏览器里的实际观感这几条管不到。**
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from html.parser import HTMLParser
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.overview import RARE_SLOTS
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

#: 摘要那一行该摆的三样，**按 `RARE_SLOTS` 的顺序**（5 合金碎片 / 8 泰坦立方 /
#: 9 收割者碎片）。逐条写死，理由同上：少一样、或者数字安错名字，都得红。
EXPECTED_PEEK_RARE = ("合金碎片 183", "泰坦立方 12", "收割者碎片 4")

#: 「战果」是表头里的第 9 列（1 起数）。同 `test_attack_log_width.py`，跟着列序走。
OUTCOME_COLUMN = 9


def _client(
    tmp_path: Path,
    *,
    resources: tuple[BattleResourceEntry, ...] = FULL_HAUL,
    with_screenshot: bool = True,
) -> TestClient:
    engine = create_database_engine(scratch_database_url(tmp_path, "outcome-dialog.db"))
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
        factory, plan_id=plan.id, idempotency_key="dialog-0001", created_at_utc=DISPATCHED
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


def _page(tmp_path: Path, **kwargs: object) -> str:
    return _client(tmp_path, **kwargs).get("/logs").text  # type: ignore[arg-type]


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
    """解析出来的一个标签，连同它头上那串祖先的标签名 / class / id。"""

    def __init__(
        self,
        tag: str,
        attrs: dict[str, str | None],
        ancestors: tuple[str, ...],
        ancestor_tags: tuple[str, ...],
    ) -> None:
        self.tag = tag
        self.attrs = attrs
        self.classes = tuple((attrs.get("class") or "").split())
        #: 从外到内，每一层祖先身上的 class 与 id 全都摊平在这里。
        self.ancestors = ancestors
        #: 同上，但摊的是标签名——判「在不在 `<template>` 里」要的是这个。
        self.ancestor_tags = ancestor_tags
        #: 这个标签范围内的文字（含后代的）。
        self.text = ""


class _Tree(HTMLParser):
    """把一段 HTML 解析成「谁在谁里面」。

    ⚠️ **不能拿字符串包含关系代替嵌套关系。** 这一批里有两条全靠它：
    「详情在不在 `<template>` 里」（决定这一行会不会被撑高）和「弹窗在不在
    `#log-entries` 外面」（决定自动刷新会不会把它掀掉）。把它们挪成兄弟节点，
    任何 `in` 断言都照样绿，而实机上行高和弹窗已经坏了。
    """

    def __init__(self, fragment: str) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[_Element] = []
        self._stack: list[_Element] = []
        self.feed(fragment)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names: list[str] = []
        for frame in self._stack:
            names.extend(frame.classes)
            identifier = frame.attrs.get("id")
            if identifier:
                names.append(f"#{identifier}")
        element = _Element(
            tag, dict(attrs), tuple(names), tuple(frame.tag for frame in self._stack)
        )
        self.elements.append(element)
        # `<img>` 之类的空元素没有闭合标签，压进栈就再也弹不出来了，
        # 后面所有元素的祖先链会跟着错。
        if tag not in {"img", "br", "hr", "input", "meta", "link", "source"}:
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
        assert len(found) == 1, f"`.{class_name}` 出现了 {len(found)} 次，期望正好一次"
        return found[0]

    def all(self, class_name: str) -> list[_Element]:
        return [item for item in self.elements if class_name in item.classes]


# -- 一、收起态那行摘要：核心三样的数量 -------------------------------------------


def test_the_summary_names_the_three_resources_the_user_actually_watches(
    tmp_path: Path,
) -> None:
    """摘要写的是**三样各自的数量**，不再是「收获 12 项」。

    用户口径 2026-08-19：「收货 12 项，改成核心 3 资源数量」。三样定在
    2026-08-17：「我最关注的是合金碎片/泰坦立方/收割者碎片，其他资源可以忽略
    不计」。「12 项」只答了「有没有」，而扫读这一列时要一眼看见的是**捞到了多少**。

    ⚠️ 逐样写死。少一样、或者数字安在了别的名字下，都得在这里红——后者是这一
    列最安静的失败方式（`domain.battle_resources` 模块头）。
    """
    peek = _Tree(_outcome_cell(_page(tmp_path))).one("outcome-peek").text

    for text in EXPECTED_PEEK_RARE:
        assert text in peek, f"摘要里少了「{text}」——核心三样必须都在"
    assert "收获 12 项" not in peek, "摘要还写着「收获 12 项」，没换成三样的数量"


def test_the_summary_shows_the_three_in_the_order_the_constant_gives(tmp_path: Path) -> None:
    """三样的槽位取自 `domain.overview.RARE_SLOTS`，模板里不许另写一遍 `(5, 8, 9)`。

    ⚠️ 抄一份的那天，加一样稀有资源就会让某一格从两边同时消失或者重复算一次
    （那张表自己的注释写着这件事）。这里从常量倒推期望顺序，所以常量一改、
    页面没跟上就红。
    """
    peek = _Tree(_outcome_cell(_page(tmp_path))).one("outcome-peek").text
    positions = [peek.find(text) for text in EXPECTED_PEEK_RARE]

    assert all(position != -1 for position in positions)
    assert positions == sorted(positions), "摘要里三样的次序和 `RARE_SLOTS` 对不上"
    assert RARE_SLOTS == (5, 8, 9), "`RARE_SLOTS` 变了：这几条用例里写死的名字与数量要跟着改一遍"


def test_a_rare_slot_with_no_row_reads_as_zero(tmp_path: Path) -> None:
    """三样里缺哪一样，摘要写 `0`——**这里确实知道它是 0，不是「不知道」**。

    依据在入库那一侧：12 格是一起读的，但凡有一格读不出来，这份战报**一行都
    不写**（`domain.battle_resources.parse_resource_grid` 与
    `storage.models.BattleReportResourceRow`）。所以「有若干行、偏偏没有 slot 8
    那一行」只有一种解释——那一格读到了，是 0。

    ⚠️ 仓库里那条「『不知道』和『是 0』不许长得一样」在这里没有被破：这一行的
    「不知道」是另一种情形，下一条用例管它。
    """
    haul = tuple(item for item in FULL_HAUL if item.slot != 8)

    peek = _Tree(_outcome_cell(_page(tmp_path, resources=haul))).one("outcome-peek").text

    assert "泰坦立方 0" in peek, "缺的那一样没写成 0"
    assert "泰坦立方 —" not in peek, "写成了「—」——那是「不知道」，而这里知道它是 0"
    assert "合金碎片 183" in peek and "收割者碎片 4" in peek, "另外两样不该跟着受影响"


def test_a_report_with_no_resource_rows_claims_nothing_at_all(tmp_path: Path) -> None:
    """一行收获都没有的战报：三样**整段不写**，不摆一排 0。

    ⚠️ 这才是真正分不开的那种（`BattleReportResourceRow` 的注释写着）：可能是
    12 格全 0 的一发白打，也可能是这条链路压根没读过资源——存量战报全是后者。
    摆一排「合金碎片 0 · 泰坦立方 0 · 收割者碎片 0」就是拿「不知道」冒充 0，
    正是那条规矩要挡的东西。
    """
    cell = _outcome_cell(_page(tmp_path, resources=()))

    assert "战损 我 0 · 敌 1120" in cell, "战损不该跟着一起消失"
    for name in ("合金碎片", "泰坦立方", "收割者碎片"):
        assert name not in cell, f"没有收获数据的行也报了「{name}」——那是把不知道说成了 0"


def test_the_summary_keeps_the_precision_marks(tmp_path: Path) -> None:
    """摘要里的近似值照样带「约」，误差范围照样在 `title` 上。

    这三样平时是几十几百的精确读数，但破千之后画面上一样是缩写显示的
    （`1.2K`）。⚠️ 把 ±50 的估算显示成确数，比不显示更糟（仓库里已有先例：
    `military_score_estimated`）；从浮层挪到摘要不是把精度声明丢掉的理由。
    """
    haul = tuple(
        BattleResourceEntry(slot=5, amount=1_200, approximate=True, uncertainty=50)
        if item.slot == 5
        else item
        for item in FULL_HAUL
    )

    tree = _Tree(_outcome_cell(_page(tmp_path, resources=haul)))
    items = {
        item.text.strip(): (item.attrs.get("title") or "") for item in tree.all("outcome-peek-item")
    }

    assert "合金碎片 约 1,200" in items, "摘要里的近似值没标「约」"
    assert items["合金碎片 约 1,200"] == "画面上是缩写显示的，误差不超过 ±50", (
        "误差范围那个 title 丢了——「约」自己说不出准到什么程度"
    )
    # 「约」和 title 必须是同一批：文字上说「约」而 title 说「精确读数」（或者
    # 反过来）的话，页面自己在打自己的脸。
    for text, hint in items.items():
        assert ("约" in text) == ("缩写" in hint), f"{text!r} 的「约」和 title 对不上：{hint!r}"


# -- 二、行高：详情整块待在 `<template>` 里，根本不参与渲染 -----------------------


def test_the_detail_lives_in_a_template_so_it_cannot_grow_the_row(tmp_path: Path) -> None:
    """详情装在 `<template>` 里——**它有多高都不可能改变行高**。

    这一条钉的就是用户 2026-08-19 那句「保持列的高度不变」。把它从 `<template>`
    里挪出来（哪怕只是换成一块 `hidden` 的 div，再哪怕忘了给它 `display: none`），
    12 项收获与那张截图立刻回到文档流里，这一行原样被撑高，整张表跟着跳。
    """
    tree = _Tree(_outcome_cell(_page(tmp_path)))

    detail = tree.one("outcome-pop")
    assert "template" in detail.ancestor_tags, (
        "详情跑出 `<template>` 了——它会跟着参与渲染，这一行又要被撑高"
    )
    assert tree.one("outcome-source").tag == "template"


def test_the_collapsed_summary_never_grows(tmp_path: Path) -> None:
    """收起态那一行摘要永远是一行：`nowrap` + 省略号，且没有任何 hover 覆盖。

    此前撑高的元凶是 `tr:hover .log-line { white-space: pre-wrap }`——一条把
    「单行省略」在 hover 时翻掉的规则。这一条盯着同一个形状别再出现：摘要上
    唯一那条 hover 规则里只许换颜色，不许出现任何改尺寸的声明。

    摘要里那三样各挂着自己的 `title`，所以它们也不许 `display: block`——
    一样一行就是三行。
    """
    css = _console_css()
    peek = _rule_block(css, ".outcome-peek {")

    assert "white-space: nowrap" in peek
    assert "text-overflow: ellipsis" in peek

    item = _rule_block(css, ".outcome-peek-item {")
    assert "display: block" not in item, "摘要里三样各占一行，行高又变了"

    hover = _rule_block(css, ".outcome-detail:hover > .outcome-peek {")
    assert _declarations(hover) == {"border-bottom-color"}, (
        f"摘要的 hover 规则里多了别的声明：{sorted(_declarations(hover))}——行高会跟着 hover 变"
    )


def test_the_outcome_column_no_longer_uses_the_row_hover_expansion(tmp_path: Path) -> None:
    """这一格里不许再出现 `.log-line`。

    那个类带着 `tr:hover .log-line { white-space: pre-wrap }`，正是把一行撑到
    半屏的那条规则。它本身还留在样式表里给系统日志用（那一页的判据在
    `test_attack_log_width.py`），所以只能从这一格的 HTML 上挡。
    """
    assert "log-line" not in _outcome_cell(_page(tmp_path))


def test_the_flip_script_is_gone(tmp_path: Path) -> None:
    """上一版那段「下面放不下就把浮层翻上去」的脚本**已经删干净**。

    它存在的唯一理由是 `.table-wrap` 那个 `overflow: auto` 会把绝对定位的浮层
    裁掉。`<dialog>` 由 `showModal()` 提到 top layer，祖先的 `overflow` 根本裁
    不到它——留着那十几行就是一段永远不会生效的死代码，而死代码读起来像是
    「这里还有一套定位逻辑」。
    """
    page = _page(tmp_path)

    assert "data-drop" not in page, "翻转脚本还在页面上"
    # ⚠️ 判 CSS 一律先剥注释，**「不许出现」这一向也不例外**：不剥的话，一句
    # 解释「那条 `[data-drop="up"]` 为什么删掉了」的注释就会把这条判红——
    # 于是下一个人只好把那句解释也删掉，而那正是最该留下的一句话。
    assert "data-drop" not in _console_css(), '配套那条 `[data-drop="up"]` 规则还在'
    assert "offsetHeight" not in page, "还在量浮层高度——那是翻转脚本才需要的"


# -- 三、点开的是原生 `<dialog>`，键盘与背景关闭都是白拿的 ------------------------


def test_the_trigger_is_a_real_button(tmp_path: Path) -> None:
    """触发元素是真的 `<button>`，不是挂 `tabindex` 的 span。

    ⚠️ Enter / 空格、焦点顺序、屏幕阅读器里的「按钮」角色全是浏览器给的。
    上一版那个 `tabindex="0"` 的 span 是为纯 CSS hover 浮层凑出来的替代品，
    改成弹窗之后再用它，等于自己去补一遍浏览器本来就做对的事。
    """
    trigger = _Tree(_outcome_cell(_page(tmp_path))).one("outcome-detail")

    assert trigger.tag == "button", "触发元素不是 `<button>`，键盘上的行为要自己补"
    assert trigger.attrs.get("type") == "button", (
        '没写 `type="button"`，将来这一格挪进 `<form>` 里就会变成提交按钮'
    )
    assert trigger.attrs.get("aria-haspopup") == "dialog"


def test_the_dialog_is_native_and_page_local(tmp_path: Path) -> None:
    """弹窗是原生 `<dialog>`，整页只有一个，ESC 与焦点陷阱都由浏览器负责。

    ⚠️ 这一页没有构建步骤，**不许为这块内容引入任何框架或外部资源**。

    ⚠️ 弹窗里**不许出现 `<form>`**，哪怕 `<form method="dialog">` 那种零 JS 就能
    关窗的写法。这一页另有一条判据钉着「最后一个 `<form>` 排在第一个
    `data-refresh` 之前」，用来保证筛选表单没被圈进自动刷新
    （`tests/e2e/test_console_auto_refresh.py`）——弹窗在页面末尾，里面放个
    `<form>` 会让那条判据永远红。关闭按钮少的那点便利，一行 `dialog.close()`
    就补上了。
    """
    tree = _Tree(_page(tmp_path))
    dialogs = [item for item in tree.elements if item.tag == "dialog"]

    assert len(dialogs) == 1, f"页面上有 {len(dialogs)} 个 `<dialog>`，期望正好一个（整页共用）"
    assert dialogs[0].attrs.get("id") == "outcome-dialog"
    assert not [
        item
        for item in tree.elements
        if item.tag == "form" and "outcome-dialog-frame" in item.ancestors
    ], "弹窗里有 `<form>`——自动刷新那条判据会跟着红"

    closers = [item for item in tree.elements if "data-outcome-close" in item.attrs]
    assert len(closers) == 1 and closers[0].tag == "button", "没有关闭按钮"

    # 接线脚本是内联的，没有 `src`：这一页没有构建步骤，外链一个都不许有。
    scripts = [item for item in tree.elements if item.tag == "script" and item.attrs.get("src")]
    assert not [
        item for item in scripts if (item.attrs.get("src") or "").startswith(("http", "//"))
    ], "页面上出现了外部脚本"


def test_clicking_the_backdrop_closes_the_dialog(tmp_path: Path) -> None:
    """点背景关闭：靠「target 就是 dialog 自己」判，所以 dialog 必须 `padding: 0`。

    ⚠️ 往 `<dialog>` 上加内边距，那圈内边距就变成一条点了会关窗的死区——
    内容整块包在 `.outcome-dialog-frame` 里正是为了这件事。
    """
    page = _page(tmp_path)

    assert "event.target === dialog" in page and "dialog.close()" in page, "点背景关闭那一条没了"
    assert "padding: 0;" in _rule_block(_console_css(), ".outcome-dialog {"), (
        "dialog 自己有内边距了，那一圈会变成点了就关窗的死区"
    )


# -- 四、12 项资源是被折叠，不是被删掉 -------------------------------------------


def test_every_resource_line_is_still_in_the_dom(tmp_path: Path) -> None:
    """12 项收获一项不少地留在弹窗那块 `<template>` 里。

    ⚠️ **摘要缩到三样是摘要，不是删数据。** 改完摘要之后最省事的做法是把
    `<template>` 里也只留那三项，页面看着一样清爽，而用户查故障时要的正是
    其余那几项。战损两个数同理。
    """
    cell = _outcome_cell(_page(tmp_path))
    tree = _Tree(cell)

    items = tree.all("outcome-haul-item")
    assert len(items) == 12, f"弹窗里只剩 {len(items)} 项收获，折叠不该删数据"
    for item in items:
        assert "outcome-pop" in item.ancestors, "收获跑到弹窗内容之外了"
        assert "template" in item.ancestor_tags, "收获跑出 `<template>` 了，这一行会被撑高"
    for text in EXPECTED_HAUL:
        assert text in cell, f"弹窗里少了 {text}——折叠不该删数据"
    assert "战损 我 0 · 敌 1120" in cell


def test_the_precision_marks_survive_the_move_into_the_dialog(tmp_path: Path) -> None:
    """近似值上的「约」与 `title` 里的误差范围跟着一起搬进弹窗。

    ⚠️ 把 ±500 的估算显示成确数，比不显示更糟（仓库里已有先例：
    `military_score_estimated`）。改版最容易顺手带走的就是这两样——它们
    一个在文字里、一个在属性里，重排 DOM 时都不显眼。
    """
    tree = _Tree(_outcome_cell(_page(tmp_path)))

    hints = [item.attrs.get("title") or "" for item in tree.all("outcome-haul-item")]
    assert "画面上是缩写显示的，误差不超过 ±500" in hints, "误差范围那个 title 丢了"
    assert "精确读数" in hints, "精确读到的那几项也该说清楚自己是精确的"
    approximate = [item for item in tree.all("outcome-haul-item") if "约" in item.text]
    assert len(approximate) == 4, "带「约」的项数不对——近似值被当成确数显示了"
    for item in approximate:
        assert "缩写" in (item.attrs.get("title") or ""), (
            f"{item.text!r} 标了「约」，title 却说它是确数"
        )


# -- 五、战报截图：内嵌进弹窗，但只在弹窗打开时才去取 -----------------------------


def test_the_screenshot_is_embedded_in_the_dialog(tmp_path: Path) -> None:
    """截图**内嵌**在弹窗里，不再只给一个跳新标签页的链接。

    这是这次改动真正值钱的那一半：光把 hover 换成点击是净亏（多了两次点击），
    「看数字」和「看原图」合成一个动作才划算（用户口径 2026-08-19）。
    """
    tree = _Tree(_outcome_cell(_page(tmp_path)))

    images = [item for item in tree.elements if item.tag == "img"]
    assert len(images) == 1, f"这一格里有 {len(images)} 张图，期望正好一张（战报截图）"
    assert "outcome-pop" in images[0].ancestors, "截图不在弹窗内容里"
    assert (images[0].attrs.get("alt") or "").strip(), "截图没有 alt"

    links = [item for item in tree.elements if item.tag == "a"]
    assert len(links) == 1, "「在新标签页看原图」那个链接不见了——弹窗里缩过的图看不清细节"
    href = links[0].attrs.get("href") or ""
    assert re.fullmatch(r"/api/reports/[0-9a-f-]{36}/screenshot", href), (
        f"原图链接的地址成了 {href!r}"
    )
    UUID(href.split("/")[3])


def test_the_screenshot_is_not_fetched_until_the_dialog_opens(tmp_path: Path) -> None:
    """列表里那张图只有 `data-src`，**没有 `src`**——不点开就一个字节都不取。

    ⚠️ 一页几十行、每张约 40 KB，真让它们在列表里加载就是几 MB 的首屏。
    这条约束在上一版是「只放链接不放 `<img>`」，换成弹窗不等于它消失了，
    只是换了个满足方式：`data-src` → `src` 在打开那一刻才发生。

    （`<template>` 本身已经是第二道保险：它的内容不解析成真元素，连 `src` 都
    不会去取。两道都留着——哪天有人把 `<template>` 换成隐藏 div，`data-src`
    还挡得住。）
    """
    page = _page(tmp_path)
    image = next(item for item in _Tree(_outcome_cell(page)).elements if item.tag == "img")

    assert image.attrs.get("src") is None, "列表里那张图直接挂了 `src`，首屏会去取它"
    assert (image.attrs.get("data-src") or "").endswith("/screenshot"), (
        "`data-src` 没了，脚本打开弹窗时就不知道该取哪张图"
    )
    assert "image.src = image.dataset.src" in page, (
        "打开弹窗时没有把 `data-src` 搬到 `src` 上——那张图永远加载不出来"
    )


def test_a_row_without_a_screenshot_offers_no_link(tmp_path: Path) -> None:
    """没有截图的那一发：既不摆图，也不摆一个点开是 404 的链接。

    ⚠️ 摆一个打不开的链接，比没有链接更难排查（上一版就是这个口径）。
    """
    cell = _outcome_cell(_page(tmp_path, with_screenshot=False))
    tree = _Tree(cell)

    assert not [item for item in tree.elements if item.tag == "img"], "没有截图的行摆了图"
    assert not [item for item in tree.elements if item.tag == "a"], "没有截图的行摆了链接"
    assert "/api/reports/" not in cell, "截图地址还留在这一格里"
    assert "有战报截图" not in cell, "摘要还说这一行有截图"
    # 收获照常——这一行仍然有东西可展开。
    assert "outcome-detail" in cell and "outcome-source" in cell


# -- 六、自动刷新每 15 秒换掉整块行，弹窗必须活下来 -------------------------------


def test_the_dialog_lives_outside_the_block_that_auto_refresh_replaces(tmp_path: Path) -> None:
    """`<dialog>` 在 `#log-entries` **外面**。

    ⚠️ 那一块每 15 秒被整块 `innerHTML` 换掉（`base.html` 的 `autoRefresh()`）。
    弹窗如果长在行里，用户正读着的那一刻就会被连根拔掉；更糟的是浏览器按
    「有没有打开的 dialog」维护背景 `inert` 与滚动锁，掀掉一个开着的 dialog
    可能把页面留在一个不上不下的状态。

    按解析出来的树判，不按字符串位置判：`<dialog>` 挪进那一块里，它在 HTML 里
    仍然出现，任何 `in` 断言都照样绿。
    """
    tree = _Tree(_page(tmp_path))
    dialog = next(item for item in tree.elements if item.tag == "dialog")

    assert "#log-entries" not in dialog.ancestors, (
        "弹窗长在 `#log-entries` 里了——自动刷新会把开着的它整块掀掉"
    )


def test_the_wiring_survives_the_rows_being_replaced(tmp_path: Path) -> None:
    """接线走 document 级事件委托，那段 `<script>` 也在 `#log-entries` 外面。

    ⚠️ 两件事一起才成立：绑在按钮上的监听会跟着行一起被换掉，而 `innerHTML`
    塞进来的 `<script>` 浏览器**根本不执行**——补都补不回来。刷新之后新长出来
    的那些行必须照样点得开。
    """
    page = _page(tmp_path)
    tree = _Tree(page)

    scripts = [
        item for item in tree.elements if item.tag == "script" and "outcome-dialog" in item.text
    ]
    assert scripts, "找不到给弹窗接线的那段脚本"
    for script in scripts:
        assert "#log-entries" not in script.ancestors, (
            "接线脚本在 `#log-entries` 里——`innerHTML` 塞进来的 `<script>` 不会执行"
        )

    assert "document.addEventListener('click'" in page, (
        "点击不是委托在 document 上——刷新之后新长出来的行点不开"
    )
    assert ".outcome-detail')" in page, "委托里没有按 `.outcome-detail` 找触发元素"


# -- 七、没东西可展开时不摆一个空按钮 ---------------------------------------------


def test_a_row_with_nothing_to_expand_gets_no_trigger(tmp_path: Path) -> None:
    """既没收获也没截图的那一行：摘要照写，但不做成能点开的东西。

    ⚠️ 摆一个点开是空的「ⓘ」，比没有它更难判断「到底是没捞着还是没记」——
    同「目标军力」那一格里角标的理由（`logs.html` 上的注释）。
    """
    cell = _outcome_cell(_page(tmp_path, resources=(), with_screenshot=False))

    assert "战损 我 0 · 敌 1120" in cell, "战损两个数不该跟着一起消失"
    assert "outcome-detail" not in cell, "没东西可展开的行也挂上了触发按钮"
    assert "outcome-source" not in cell
    assert "outcome-more" not in cell


# -- 样式表读取：判之前先把注释剥掉 -----------------------------------------------


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
    `tr:hover .log-line`、`padding: 0` 都被引用了不止一处）。不剥的话两头都会
    说假话：「这条规则还在」会被一句谈论它的注释喂饱，「这条规则里不许出现 X」
    会被一句解释 X 为什么不能用的注释判红。
    """
    return re.sub(r"/\*.*?\*/", "", _console_css_raw(), flags=re.DOTALL)


def _rule_block(css: str, selector: str) -> str:
    """把某条规则的声明块取出来（含选择器那一行）。"""
    start = css.find(selector)
    assert start != -1, f"console.css 里找不到 `{selector}`"
    end = css.find("}", start)
    assert end != -1, f"`{selector}` 那条规则没有收尾的大括号"
    return css[start:end]


def _declarations(block: str) -> set[str]:
    return {line.split(":")[0].strip() for line in block.split("{")[1].split(";") if ":" in line}
