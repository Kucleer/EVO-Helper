"""攻击日志的「战果」列：折叠起来，但一个数字都不许少。

用户 2026-08-18 报：`/logs` 被撑得太宽，底下拖出一条很长的横向滚动条，左边的
「发动时间 / 事件类型 / 目标 / 出发 / 预设 / 结果」全被挤到看不全。量出来的元凶
就是这一列——战损一行加**12 项**资源摊开是 945px，把整张表顶到 1933px。

做法照系统日志页（`/system-log`，PR #170）那一套：同样的 `.log-body` /
`.log-line` 两个类，同样的 `tr:hover` 展开。

⚠️ **这一页的判据是「只折叠、不删」。** 截断纯粹发生在 CSS 里，HTML 里那 12 项
必须一项不少——用户查故障时最需要的就是这些数字，鼠标停在那一行就要全都在。
真去截字符串的话，页面会显得「修好了」，而丢掉的正是这一列存在的理由。

CSS 本身在这里测不了（没有浏览器），所以分两条钉：一条钉数据完整，一条钉那一格
确实挂上了限宽用的类——两条都在，才既没删数据、又真的折叠了。
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


def _outcome_cell(html: str) -> str:
    """把表格体里「战果」那一格的原样 HTML 取出来。

    整页搜会命中顶上那几个下拉框；只看这一格，断言说的才是这一格的事。
    """
    start = html.find("<tbody")
    assert start != -1, "页面上没有表格体，这几条用例的前提就不成立"
    cell = re.search(r'<td class="log-body">(.*?)</td>', html[start:], re.DOTALL)
    assert cell is not None, '表格体里找不到「战果」那一格（`<td class="log-body">`）'
    return cell.group(1)


# -- 一、只折叠，不删 ----------------------------------------------------------


def test_the_whole_haul_survives_the_fold(tmp_path: Path) -> None:
    """12 项资源与战损两个数**全部**留在 HTML 里。

    ⚠️ 这一条钉的就是「别真去截字符串」。折叠只许发生在 CSS 里（`.log-line` 的
    省略号 + `tr:hover` 展开）；只要有人图省事在模板或视图里把正文截短，页面
    看着一样清爽，而用户排查时要的那几个数字就永远拿不回来了。
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
    """「战果」那一格用的是系统日志页同一套类，不是另起的一份。

    `.log-body` 限死列宽、`.log-line` 单行省略号、`tr:hover .log-line` 展开——
    三条规则在 console.css 里共用（见那里的注释）。这一格少挂一个类，页面就退回
    2026-08-18 报的那个样子：整张表 1933px 宽，左边六列全被挤出视野。
    """
    html = _client(tmp_path).get("/logs").text
    cell = _outcome_cell(html)

    # 战损与收获两行都要能截断——只收拾其中一行，另一行照样把列撑开。
    assert cell.count('class="log-line muted"') == 2


def test_the_shared_truncation_rules_are_actually_in_the_stylesheet(tmp_path: Path) -> None:
    """光在 HTML 上挂类名没有用：规则不在样式表里，那两个类就只是装饰。

    连同这一页自己的那个收窄值一起钉住——系统日志只有 7 列，它的 46vw 放到这张
    9 列表上仍然溢出（1920 下量到 1854px > 容器 1650px），所以这一页必须自己给
    `--log-body-cap` 一个值。
    """
    css = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "evo_helper"
        / "web"
        / "static"
        / "console.css"
    ).read_text(encoding="utf-8")

    assert ".log-body { max-width: var(--log-body-cap" in css
    assert "text-overflow: ellipsis" in css
    assert "tr:hover .log-line" in css
    assert "#log-entries { --log-body-cap:" in css
    # 网格子项默认不肯比内容窄，宽表格会把**整页**顶宽——那是这次的另一半元凶。
    assert "grid-template-columns: minmax(0, 1fr)" in css
