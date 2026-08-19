"""攻击日志的「战果」列：折叠起来，但一个数字都不许少。

用户 2026-08-18 报：`/logs` 被撑得太宽，底下拖出一条很长的横向滚动条，左边的
「发动时间 / 事件类型 / 目标 / 出发 / 预设 / 结果」全被挤到看不全。量出来的元凶
就是这一列——战损一行加**12 项**资源摊开是 945px，把整张表顶到 1933px。

限宽照系统日志页（`/system-log`，PR #170）那一套：同一个 `.log-body`。
**展开不照它**——那一页 `tr:hover` 换行展开，而这一列展开是浮层
（用户 2026-08-19：「像 tips 那种 hover 效果，而不是这种增加列高的」）。
浮层那一套自己的判据在 `test_attack_log_outcome_popover.py`。

⚠️ **这一页的判据是「只折叠、不删」。** 截断纯粹发生在 CSS 里，HTML 里那 12 项
必须一项不少——用户查故障时最需要的就是这些数字，鼠标停在那一行就要全都在。
真去截字符串的话，页面会显得「修好了」，而丢掉的正是这一列存在的理由。

CSS 本身在这里测不了（没有浏览器），所以分两条钉：一条钉数据完整，一条钉那一格
确实挂上了限宽用的类——两条都在，才既没删数据、又真的折叠了。

第三节是 PR #183 加的：新增「目标军力」一列之后，这一页**不许把上面那件事撞回去**。
没有浏览器量不了真实布局，所以照着 console.css 里那几个标定量把版面**算一遍**——
定宽列的量值加上「战果」列的上限，在 1280 与 1920 下都要放得进容器。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

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

#: 12 格全部非零——正是把这一页撑破的那种记录。前四项是画面上缩写显示的近似值，
#: 后八项是逐位读到的精确数（两种写法都要活到 HTML 里）。
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


def _client(tmp_path: Path) -> TestClient:
    engine = create_database_engine(scratch_database_url(tmp_path, "wide-logs.db"))
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
        factory, plan_id=plan.id, idempotency_key="wide-log-0001", created_at_utc=DISPATCHED
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
    repository.append_report(
        BattleReport(
            report_id=uuid4(),
            reported_at_utc=DISPATCHED + timedelta(minutes=43),
            attacker_origin=ORIGIN,
            defender_target=TARGET,
            raw_time_text="17/08/2026 04:38:46",
            outcome=OUTCOME_VICTORY,
            attacker_losses=0,
            defender_losses=1120,
            resources=FULL_HAUL,
        )
    )
    return TestClient(create_persistent_app(factory))


#: 「战果」是表头里的第 9 列（1 起数）。
#:
#: PR #183 在「目标」后面插了「目标军力」，它此前是第 8 列。**这个常量跟着列序走**
#: ——不跟的话下面三条会去量「预设」那一格，然后以「没挂 `.log-body`」红掉，
#: 而真正的毛病其实在别处。
OUTCOME_COLUMN = 9


def _outcome_cell(html: str) -> str:
    """把表格体第一行里「战果」那一格的原样 HTML 取出来。

    ⚠️ **按列的位置取，不按类名取。** 整页搜会命中顶上那几个下拉框；而如果这里
    改成搜 `class="log-body"`，「数据没被删」那两条就会跟着限宽一起红——两组用例
    说的是两件事，混在一起就分不清是谁坏了。
    """
    start = html.find("<tbody")
    assert start != -1, "页面上没有表格体，这几条用例的前提就不成立"
    row = re.search(r"<tr[^>]*>(.*?)</tr>", html[start:], re.DOTALL)
    assert row is not None, "表格体里一行都没有，这几条用例的前提就不成立"
    cells = row.group(1).split("<td")
    assert len(cells) > OUTCOME_COLUMN, f"这一行只有 {len(cells) - 1} 格，取不到「战果」那一列"
    return cells[OUTCOME_COLUMN]


# -- 一、只折叠，不删 ----------------------------------------------------------


def test_the_whole_haul_survives_the_fold(tmp_path: Path) -> None:
    """12 项资源与战损两个数**全部**留在 HTML 里。

    ⚠️ 这一条钉的就是「别真去截字符串」。折叠只许发生在 CSS 里（收起态那一行
    摘要 + 浮层里的全文）；只要有人图省事在模板或视图里把正文截短，页面看着
    一样清爽，而用户排查时要的那几个数字就永远拿不回来了。
    """
    cell = _outcome_cell(_client(tmp_path).get("/logs").text)

    for item in EXPECTED_HAUL:
        assert item in cell, f"「战果」那一格里少了 {item}——折叠不该删数据"
    assert "战损 我 0" in cell
    assert "敌 1120" in cell


def test_the_fold_does_not_swallow_the_precision_marks(tmp_path: Path) -> None:
    """近似值上的「约」与 title 里的误差范围也要一起活下来。

    截断最容易顺手带走的就是行尾那几项和它们的 title——而把 ±500 的估算显示成
    确数，比不显示更糟（仓库里已有先例：`military_score_estimated`）。
    """
    cell = _outcome_cell(_client(tmp_path).get("/logs").text)

    assert "误差不超过 ±500" in cell
    assert "银河石能量 9" in cell


# -- 二、那一格真的挂上了限宽 ----------------------------------------------------


def test_the_outcome_column_reuses_the_shared_width_cap(tmp_path: Path) -> None:
    """「战果」那一格仍然挂着系统日志页那个限宽类，不是另起的一份。

    `.log-body` 限死列宽（console.css 里两页共用）。这一格少挂它，页面就退回
    2026-08-18 报的那个样子：整张表 1933px 宽，左边六列全被挤出视野。

    ⚠️ **只有限宽是共用的，展开不是。** 这一格不许再出现 `.log-line`——那个类
    带着 `tr:hover` 换行展开，正是 2026-08-19 那个「一行被撑到半屏」的来源。
    """
    cell = _outcome_cell(_client(tmp_path).get("/logs").text)

    assert cell.startswith(' class="log-body log-outcome">'), (
        "「战果」那一格没挂 `.log-body`，列宽就没有上限"
    )
    assert "log-line" not in cell, "「战果」那一格又挂回了 `.log-line`，hover 会把这一行撑高"


def _console_css() -> str:
    """样式表，**注释先剥掉**。

    ⚠️ 这个仓库里注释比代码长，而注释里成段引用着规则本身（`tr:hover .log-line`
    在 2026-08-19 那次改动之后就被引用了两处）。不剥的话，「这条规则还在不在」
    的断言会被一句谈论它的注释喂饱——真把规则删了也照样绿，用例说的是假话。
    """
    css = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "evo_helper"
        / "web"
        / "static"
        / "console.css"
    ).read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def test_the_shared_truncation_rules_are_actually_in_the_stylesheet(tmp_path: Path) -> None:
    """光在 HTML 上挂类名没有用：规则不在样式表里，那个类就只是装饰。

    连同这一页自己的那个收窄值一起钉住——系统日志只有 7 列，它的 46vw 放到这张
    9 列表上仍然溢出（1920 下量到 1854px > 容器 1650px），所以这一页必须自己给
    `--log-body-cap` 一个值。

    末一条钉的是「别顺手把两页统一掉」：`tr:hover .log-line` 是**系统日志**那一列
    的展开方式，它那一格里的 base64 缩略图本来就靠 hover 放大（`.log-shot`）。
    攻击日志改成浮层时把它一并删掉，系统日志的正文就再也展不开了。
    """
    css = _console_css()

    assert ".log-body { max-width: var(--log-body-cap" in css
    assert "text-overflow: ellipsis" in css
    assert "#log-entries { --log-body-cap:" in css
    assert "tr:hover .log-line" in css, "系统日志那一列的展开被一起删了"
    # 网格子项默认不肯比内容窄，宽表格会把**整页**顶宽——那是这次的另一半元凶。
    assert "grid-template-columns: minmax(0, 1fr)" in css


# -- 三、加了「目标军力」之后，两种屏宽下仍然放得进容器 --------------------------

#: 视口宽减去表格容器宽：左侧导航加内外边距。
#:
#: 从 console.css 里那两句量值倒推出来的同一个数——1280 下容器 1010px、1920 下
#: 1650px，两边都差 270px。它跟着屏宽不变，因为导航是定宽的。
CHROME_PX = 270

#: 折行之后那些定宽列合计多宽。
#:
#: 788px = 原先八列的 714px（console.css 里量的）+ 「目标军力」那一列 74px。
#: 新列按**最宽的那种写法**量：`1,234,567` 加一个角标，一行约 70px，留到 74px。
#: 读数时刻藏在角标的 `title` 里，不占版面。
#:
#: ⚠️ 「(估算)」（PR #184）**不加进这个量值**：那一列在折行名单里
#: （`nth-child(5)`，见下一条用例），多出来的两个字换行，不顶宽。这正是当初
#: 把它留在名单里的那道保险生效的时刻——1280 下整张表只剩 14px 余量，按
#: `1,234,567 (估算) ⓘ` 一行去留宽度，横向滚动条当场回来。
#:
#: ⚠️ 这些不是偏好项，是按版面量出来的标定量（同 console.css 里那段警告）。
FIXED_COLUMNS_PX = 714 + 74

#: 「战果」那一列的上限公式里那个百分比（`min(32vw, …)`）。
_CAP_RULE = re.compile(
    r"#log-entries \{ --log-body-cap: min\((\d+)vw, calc\(100vw - (\d+)rem\)\); \}"
)

#: `rem` 相对根元素字号；这一页没有改 `html { font-size }`，所以是浏览器默认的 16px。
REM_PX = 16


def _cap_px(viewport_px: int) -> float:
    """按 console.css 里那条公式算出「战果」列此刻的上限。"""
    match = _CAP_RULE.search(_console_css())
    assert match is not None, "console.css 里找不到 `--log-body-cap` 那条公式，核算无从谈起"
    ratio_vw, reserve_rem = int(match.group(1)), int(match.group(2))
    return min(viewport_px * ratio_vw / 100, viewport_px - reserve_rem * REM_PX)


def test_the_wrapping_columns_follow_the_new_column_order() -> None:
    """折行名单跟着列序走：「目标军力」是第 5 列，「预计战报」挪到了第 10。

    这一条钉的是「插列之后别忘了改序号」。「预计战报」留在第 9 的话，折行会落在
    「战果」那一格——它本来就折行，看不出任何异样——而真正需要折行的「预计战报」
    一声不响地把表撑宽，正是 PR #178 收拾过的那个症状。

    「目标军力」留在名单里当初是道便宜的保险，现在真用上了：PR #184 往那一格里
    加了「(估算)」，`1,234,567 (估算) ⓘ` 一行摆不下，靠的就是这条规则换行。
    掉出名单，多出来的两个字就去顶列宽——而 1280 下整张表只剩 14px 余量。
    """
    css = _console_css()
    wrap_rule = css[css.index("#log-entries th:nth-child(1)") :].split("}")[0]

    assert "nth-child(5)" in wrap_rule, "「目标军力」那一列被漏出了折行名单"
    assert "nth-child(10)" in wrap_rule, "「预计战报」的序号没跟着插列挪，折行落到了别的列上"
    assert "nth-child(9)" not in wrap_rule, "第 9 列是「战果」，它走 `.log-body`，不该在折行名单里"


def test_the_ten_column_table_still_fits_at_1280_and_1920() -> None:
    """定宽列 + 「战果」列上限，在 1280 与 1920 下都放得进容器。

    这是 PR #178 那件事的守卫：加一列而不把 `--log-body-cap` 的预留一起调大，
    新列的宽度就是白拿的，横向滚动条原样回来。1280 是最紧的那一档——1920 下
    上限被 32vw 卡住，反而宽松。

    没有浏览器量不了真实布局，所以照着 console.css 自己写下的标定量算一遍：
    公式从样式表里读，量值写在上面的常量里。有人把预留改回 62rem，这一条当场红。
    """
    for viewport in (1280, 1920):
        cap = _cap_px(viewport)
        assert cap > 0, f"{viewport} 下「战果」列的上限算成了 {cap}px，那一列会整个塌掉"
        table = FIXED_COLUMNS_PX + cap
        container = viewport - CHROME_PX
        assert table <= container, (
            f"{viewport} 下整张表 {table:.0f}px 超过容器 {container}px——横向滚动条回来了"
        )


def test_the_score_column_sits_next_to_the_target_column(tmp_path: Path) -> None:
    """表头是 10 列，且「目标军力」紧跟在「目标」后面。

    位置本身是这一列的用处的一半：它回答的是「当时凭什么打这个坐标」，摆到表尾
    就得来回扫视。顺带钉住列数——上面那两条按序号算的核算全都建立在它上面。
    """
    html = _client(tmp_path).get("/logs").text
    head = html[html.index("<thead") : html.index("</thead>")]
    headers = re.findall(r"<th[^>]*>(.*?)</th>", head, re.DOTALL)

    assert len(headers) == 10, f"表头是 {len(headers)} 列，按序号写的那些 CSS 规则要跟着改"
    assert headers[3].strip() == "目标"
    assert headers[4].strip() == "目标军力"
