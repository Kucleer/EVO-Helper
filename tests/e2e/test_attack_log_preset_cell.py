"""攻击日志「预设」那一格：同一个预设名不许出现两遍，但**不一致时必须看得见**。

用户口径（2026-08-19）：「预设栏出现了重复的预设，保留一个就可以了」。那一格
此前长这样——chip 一行 `[✗ 攻击] BBB`，底下再来一行 `预设:BBB`。

## 两个值是不是重复，是查过才敢断言的

`attack_intents.preset_name` 与 `attack_intents.preset_signature` 在**同一行**上，
但**不是同一条写入路径**：

- 海盗那条（`tools.pirate_loop._record_intent`）写的签名就是
  `domain.fleet_preset.title_signature(name)` = `预设:{标题}`，从标题推出来的，
  一个字的新信息都没有——用户看到的那种重复就是它；
- 老那条（`application.workflow`）写的是 `scan_ranges.fleet_preset_signature`，
  也就是 `舰种:数量` 那种组成签名（`domain.fleet_preset.composition_signature`，
  `tools.scan_coordinates.PRESET_SIGNATURE` 就是 `轻型战斗机:1`）。它和标题说的是
  **两件事**，不一致时正是要看出来的：「派出去的预设」和「记下的组成」对不上，
  是安全不变量 9 要挡的那类事故。

生产库实测（2026-08-19，只读事务）：1160 条意图，签名全部等于 `预设:{标题}`——
也就是今天页面上那一行确实是纯重复，删得掉。

## 所以判据是两条，不是一条

第一节钉「推得出来的那一份收起来」，第二节钉「推不出来的那一份照常显示」。
**只做第一条就是无条件删**，那样两值真的不一致时页面也不显示，等于把唯一能发现
这类事故的地方悄悄关掉。

## 第三节：行高

那一格原先永远两行，整张表因此行行等高。收起签名之后它变一行，而「战果」那一格
是有战报才两行——两下一凑，同一屏里就会出现两种行高。这一块每 15 秒整块自动刷新，
战报一到那一行就长高、下面所有行往下跳。用户对这张表的原话是「保持列的高度不变」。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.fleet_preset import composition_signature, title_signature
from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import TARGET_KIND_BOT, AttackDispatch, AttackIntent
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app
from evo_helper.web.display import preset_signature_note
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.database import scratch_database_url
from support.runs import seed_run_instance

#: 坐标一律是编出来的：这个仓是公开的，夹具里不放真实坐标。
ORIGIN = Coordinate(5, 311, 12)
BOT_TARGET = Coordinate(5, 311, 9)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
DISPATCHED = datetime(2026, 8, 18, 3, 55, tzinfo=UTC)

PRESET_NAME = "BBB"
#: 海盗链路写下去的那种签名：纯粹从标题推出来的。
DERIVED_SIGNATURE = title_signature(PRESET_NAME)
#: 老那条链路写下去的那种签名：`舰种:数量`，和标题说的是两件事。
COMPOSITION_SIGNATURE = composition_signature({"深空吞噬者": 70})

#: 「预设」是表头里的第 7 列（1 起数）。
PRESET_COLUMN = 7


def _factory(
    tmp_path: Path, signature: str
) -> tuple[SqlAlchemyRepository, UUID, sessionmaker[Session]]:
    engine = create_database_engine(scratch_database_url(tmp_path, "preset-cell.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: DISPATCHED)
    plan = service.create_plan(
        name="bot 攻击",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        ranges=(
            ScanRangeView(Coordinate(5, 311, 1), BOT_TARGET, ORIGIN, PRESET_NAME, signature, 0),
        ),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="preset-cell-1", created_at_utc=DISPATCHED
    )
    return SqlAlchemyRepository(factory), run_id, factory


def _dispatch(repository: SqlAlchemyRepository, run_id: UUID, signature: str) -> None:
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=BOT_TARGET,
        preset=FleetPresetRef(name=PRESET_NAME, signature=signature),
        cycle_start_utc=CYCLE,
        created_at_utc=DISPATCHED - timedelta(minutes=1),
        target_kind=TARGET_KIND_BOT,
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


def _preset_cell(html: str) -> str:
    """表格体第一行里「预设」那一格的原样 HTML。

    ⚠️ **只搜这一格。** 预设名同时出现在顶上那个筛选下拉框里——整页数「BBB 出现
    几次」的话，把格子里的两行都删光也数得出至少一个。
    """
    start = html.find("<tbody")
    assert start != -1, "页面上没有表格体，这几条用例的前提就不成立"
    row = re.search(r"<tr[^>]*>(.*?)</tr>", html[start:], re.DOTALL)
    assert row is not None, "表格体里一行都没有，这几条用例的前提就不成立"
    cells = row.group(1).split("<td")
    assert len(cells) > PRESET_COLUMN, f"这一行只有 {len(cells) - 1} 格，取不到「预设」那一列"
    return cells[PRESET_COLUMN]


def _visible(cell: str) -> str:
    """去掉标签，只剩渲染出来会被人读到的字。"""
    return re.sub(r"<[^>]*>", " ", cell)


def _logs(factory: sessionmaker[Session]) -> str:
    return TestClient(create_persistent_app(factory)).get("/logs").text


def _console_css() -> str:
    """样式表，注释先剥掉（同 `test_attack_log_width.py` 里那个同名助手）。"""
    css = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "evo_helper"
        / "web"
        / "static"
        / "console.css"
    ).read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


# -- 一、推得出来的那一份收起来 --------------------------------------------------


def test_a_signature_derived_from_the_title_is_not_shown_twice(tmp_path: Path) -> None:
    """⚠️ **这一条就是用户 2026-08-19 那句口径本身。**

    签名等于 `预设:{标题}` 时，它一个字的新信息都没有——那一格只留标题。
    """
    repository, run_id, factory = _factory(tmp_path, DERIVED_SIGNATURE)
    _dispatch(repository, run_id, DERIVED_SIGNATURE)

    cell = _preset_cell(_logs(factory))
    visible = _visible(cell)

    assert PRESET_NAME in visible, "把预设名整个删掉了——这一列就没有内容了"
    assert visible.count(PRESET_NAME) == 1, "预设名还是出现了两遍"
    assert DERIVED_SIGNATURE not in visible, "`预设:BBB` 那一行还在"


def test_the_kept_value_is_the_one_the_filters_key_on(tmp_path: Path) -> None:
    """留下的是**标题**，不是签名。

    三条理由：列头本来就写着「预设」，`预设:` 那个前缀等于把表头又念一遍；顶上那个
    筛选下拉框列的、SQL 里 `WHERE preset_name = ?` 匹配的，都是标题；而签名在海盗
    那条链路上就是从标题拼出来的。留反了的话，用户在下拉框里选的词和表格里看到的
    词对不上。
    """
    repository, run_id, factory = _factory(tmp_path, DERIVED_SIGNATURE)
    _dispatch(repository, run_id, DERIVED_SIGNATURE)

    html = _logs(factory)
    cell = _visible(_preset_cell(html))

    assert PRESET_NAME in cell
    assert "预设:" not in cell, "留下的是签名而不是标题——它和筛选下拉框里的词对不上"
    assert f'<option value="{PRESET_NAME}"' in html, (
        "筛选下拉框里的候选值不是标题，这条用例的前提不成立"
    )


# -- 二、推不出来的那一份照常显示 ------------------------------------------------


def test_a_signature_that_disagrees_with_the_title_is_still_shown(tmp_path: Path) -> None:
    """⚠️ **这一条挡的是「顺手改成无条件删」。**

    老那条 `application.workflow` 路径写进去的是 `舰种:数量` 组成签名。它和标题说的
    是两件事——「派出去的预设叫 BBB」和「记下的组成是深空吞噬者×70」对不上，正是
    安全不变量 9 要挡的那类事故，页面上必须看得见。

    去重去到把它也藏掉，页面不会报错，只会安静地少说一件事。
    """
    repository, run_id, factory = _factory(tmp_path, COMPOSITION_SIGNATURE)
    _dispatch(repository, run_id, COMPOSITION_SIGNATURE)

    cell = _visible(_preset_cell(_logs(factory)))

    assert PRESET_NAME in cell
    assert COMPOSITION_SIGNATURE in cell, "签名和标题对不上，页面却把签名藏起来了"


def test_the_note_helper_decides_by_the_same_rule_the_writer_uses() -> None:
    """判据和写入那一侧共用同一个函数，不是各写一份格式。

    `tools.pirate_loop._preset_signature` 和这里的判据都走
    `domain.fleet_preset.title_signature`。各写一份的话，哪天格式改了，症状是页面上
    凭空多出一列重复的预设名（或者真的对照被静默藏掉），而两边的代码看着都对。
    """
    from evo_helper.tools.pirate_loop import _preset_signature

    assert _preset_signature(PRESET_NAME) == title_signature(PRESET_NAME)
    assert preset_signature_note(PRESET_NAME, title_signature(PRESET_NAME)) is None
    assert preset_signature_note(PRESET_NAME, COMPOSITION_SIGNATURE) == COMPOSITION_SIGNATURE
    # 空签名也算「说不出东西」，但它不等于推导值，所以照实露出来——静默吞掉一个
    # 空签名，等于把「这条记录没记全」这件事也一起吞了。
    assert preset_signature_note(PRESET_NAME, "") == ""


# -- 三、去重之后行高不许塌 ------------------------------------------------------


def test_the_row_height_is_pinned_so_the_table_stops_jittering() -> None:
    """「战果」那一格有战报才两行，「预设」那一格现在恒为一行——不钉住的话同一屏里
    会出现两种行高。

    这一块每 15 秒整块自动刷新（`data-refresh`），战报一到那一行就长高、下面所有行
    往下跳，鼠标底下那一行会在点下去之前跑掉。用户对这张表的原话是「保持列的高度
    不变」。

    ⚠️ 这个数按样式表里那几个量值算：td 上下 padding 10 + chip 一行 26
    （12px×1.5 + padding 6 + border 2）+ 第二行 11px×1.5 ≈ 17，共 53。
    比它小就是真的塌了。
    """
    rule = re.search(r"#log-entries\s+tbody\s+tr\s*\{([^}]*)\}", _console_css())

    assert rule is not None, "样式表里没钉行高——去重之后这张表会两种行高混着跳"
    height = re.search(r"height:\s*(\d+)px", rule.group(1))
    assert height is not None, "行高不是一个固定的 px 值"
    assert int(height.group(1)) >= 53, "行高比两行内容还矮，等于没钉"
