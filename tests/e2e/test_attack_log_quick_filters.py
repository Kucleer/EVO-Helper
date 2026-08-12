"""攻击日志顶部的三档快速筛选：预设 / 结果 / 战果。

四件事在这里钉死：

1. **一行就是一次派遣，按这一行自己的值筛。** 情报中心那三个同名的档筛的是
   「目标星球」，所以才有「按最近一次派遣判」那套口径；搬到这一页就会把
   「今天被拦下的那几发」按星球的最新状态筛掉。
2. **筛选下推到 SQL。** 这一页只取 `ATTACK_LOG_LIMIT` 条，在内存里筛等于
   「先砍掉历史再问历史」——查一个旧预设会得到空页，而空页读起来和
   「那个预设没打过」一模一样（日期 PR #77、事件类型 PR #96 都栽在这里）。
3. **空串是「不筛」，不是 422。** 三个下拉框的「全部」那一项 value 就是空串，
   提交表单必然带上 `preset=&result=&outcome=`（PR #74 的教训）。
4. **候选值从库里现有的取。** 预设是用户自己在游戏里维护的，写死字面量就会
   漏掉他新建的那一个。
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    BattleReport,
)
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.pirate_reports import OUTCOME_FAIL, OUTCOME_VICTORY
from evo_helper.web.app import ATTACK_LOG_LIMIT, create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.runs import seed_run_instance

ORIGIN = Coordinate(2, 137, 18)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
NOW = datetime(2026, 8, 10, 6, tzinfo=UTC)
CREATED = datetime(2026, 8, 9, 12, tzinfo=UTC)

AAA = FleetPresetRef(name="AAA", signature="深空吞噬者:70")
BBB = FleetPresetRef(name="BBB", signature="轻型战斗机:1")
#: 只有那条排在 `ATTACK_LOG_LIMIT` 之外的老记录用它——下推判据全靠这一档。
OLD_PRESET = FleetPresetRef(name="ZZZ-旧预设", signature="小型运输船:1")

#: AAA · 海盗 · 已派出 · 胜
WON = Coordinate(2, 137, 1)
#: AAA · 海盗 · 已派出 · 负
LOST = Coordinate(2, 137, 2)
#: AAA · 海盗 · 已派出 · 还没战报
FLYING = Coordinate(2, 137, 3)
#: BBB · bot · 派出去被游戏拒了
REJECTED = Coordinate(2, 137, 4)
#: BBB · 海盗 · 被闸门拦下，根本没派出去
NEVER_LEFT = Coordinate(2, 137, 5)
#: ZZZ-旧预设 · 海盗 · 已派出 · 胜，且老到被 `ATTACK_LOG_LIMIT` 挡在外面
OLD = Coordinate(2, 137, 6)


def _seed(tmp_path: Path) -> tuple[PersistentApplicationService, TestClient]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'log-quick.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: NOW)
    plan = service.create_plan(
        name="海盗攻击",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        ranges=(ScanRangeView(Coordinate(2, 1, 1), Coordinate(2, 999, 20), ORIGIN, "AAA", "x", 0),),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="log-quick-0001", created_at_utc=NOW
    )
    repository = SqlAlchemyRepository(factory)

    def _intent(
        target: Coordinate, preset: FleetPresetRef, kind: str, minutes: int
    ) -> AttackIntent:
        intent = AttackIntent(
            intent_id=uuid4(),
            run_id=run_id,
            origin=ORIGIN,
            target=target,
            preset=preset,
            cycle_start_utc=CYCLE,
            created_at_utc=CREATED + timedelta(minutes=minutes),
            target_kind=kind,
        )
        repository.save_attack_intent(intent)
        return intent

    def _dispatch(intent: AttackIntent, minutes: int, *, accepted: bool = True) -> datetime:
        moment = CREATED + timedelta(minutes=minutes)
        repository.save_dispatch(
            AttackDispatch(
                dispatch_id=uuid4(),
                intent_id=intent.intent_id,
                dispatched_at_utc=moment,
                accepted=accepted,
            )
        )
        return moment

    def _report(target: Coordinate, dispatched: datetime, outcome: str) -> None:
        repository.append_report(
            BattleReport(
                report_id=uuid4(),
                reported_at_utc=dispatched + timedelta(minutes=20),
                attacker_origin=ORIGIN,
                defender_target=target,
                outcome=outcome,
                attacker_losses=0,
                defender_losses=7,
            )
        )

    # 那条被 limit 挡在外面的老记录：先造，且刻意让它成为最旧的一条。
    _seed_filler(factory, run_id)
    old = _intent(OLD, OLD_PRESET, TARGET_KIND_PIRATE, -600)
    _report(OLD, _dispatch(old, -600), OUTCOME_VICTORY)

    _report(WON, _dispatch(_intent(WON, AAA, TARGET_KIND_PIRATE, 1), 1), OUTCOME_VICTORY)
    _report(LOST, _dispatch(_intent(LOST, AAA, TARGET_KIND_PIRATE, 2), 2), OUTCOME_FAIL)
    _dispatch(_intent(FLYING, AAA, TARGET_KIND_PIRATE, 3), 3)
    _dispatch(_intent(REJECTED, BBB, TARGET_KIND_BOT, 4), 4, accepted=False)
    _intent(NEVER_LEFT, BBB, TARGET_KIND_PIRATE, 5)

    return service, TestClient(create_persistent_app(factory))


def _seed_filler(factory: sessionmaker[Session], run_id: UUID) -> None:
    """把 `ATTACK_LOG_LIMIT` 条无关记录塞满，逼出「先砍历史再问历史」那个坑。

    时间刻意夹在中间：比上面那五发旧、比 `OLD` 新。这样不筛的时候页面照样
    看得见那五发，而 `OLD` 稳稳被挤在 `ATTACK_LOG_LIMIT` 之外。

    直接建行而不走仓储：这里要的只是「日志上排在老记录前面的一堆行」，
    一条条 commit 会让这份用例慢上一个数量级。
    """
    with factory() as session:
        for index in range(ATTACK_LOG_LIMIT):
            session.add(
                orm.AttackIntentRow(
                    id=uuid4(),
                    run_id=run_id,
                    origin_galaxy=ORIGIN.galaxy,
                    origin_system=ORIGIN.system,
                    origin_position=ORIGIN.position,
                    target_galaxy=2,
                    target_system=200,
                    target_position=index + 1,
                    preset_name="填充",
                    preset_signature="填充:1",
                    cycle_start_utc=CYCLE,
                    guard_status="OK",
                    forced_revisit=False,
                    created_at_utc=CREATED - timedelta(minutes=10 + index),
                    target_kind=TARGET_KIND_PIRATE,
                )
            )
        session.commit()


def _link(target: Coordinate) -> str:
    return f"/targets/{target.galaxy}:{target.system}:{target.position}"


# ---- 空串不 422 -----------------------------------------------------------


def test_empty_quick_filters_mean_no_filter_instead_of_422(tmp_path: Path) -> None:
    """三个下拉框的「全部」那一项 value 是空串，提交表单一定会带上它们。"""
    _, client = _seed(tmp_path)

    response = client.get("/logs", params={"preset": "", "result": "", "outcome": ""})

    assert response.status_code == 200
    body = response.text
    for target in (WON, LOST, FLYING, REJECTED, NEVER_LEFT):
        assert _link(target) in body
    assert "未按预设 / 结果 / 战果筛选" in body


# ---- 三个筛选各自筛对 -----------------------------------------------------


def test_the_preset_filter_keeps_only_that_preset(tmp_path: Path) -> None:
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"preset": "AAA"}).text

    for kept in (WON, LOST, FLYING):
        assert _link(kept) in body
    for dropped in (REJECTED, NEVER_LEFT):
        assert _link(dropped) not in body


def test_the_result_filter_separates_sent_rejected_and_blocked(tmp_path: Path) -> None:
    """三档由「有没有派遣行 + accepted」算出来，和页面上那一格同一条判据。"""
    _, client = _seed(tmp_path)

    sent = client.get("/logs", params={"result": "SENT"}).text
    for kept in (WON, LOST, FLYING):
        assert _link(kept) in sent
    for dropped in (REJECTED, NEVER_LEFT):
        assert _link(dropped) not in sent

    rejected = client.get("/logs", params={"result": "REJECTED"}).text
    assert _link(REJECTED) in rejected
    for dropped in (WON, LOST, FLYING, NEVER_LEFT):
        assert _link(dropped) not in rejected

    blocked = client.get("/logs", params={"result": "BLOCKED"}).text
    assert _link(NEVER_LEFT) in blocked
    for dropped in (WON, LOST, FLYING, REJECTED):
        assert _link(dropped) not in blocked


def test_the_outcome_filter_separates_wins_losses_and_pending(tmp_path: Path) -> None:
    """`AWAITING` 覆盖「还没战报」与「战报没读出胜负」——页面上它们同样显示待战报。"""
    _, client = _seed(tmp_path)

    won = client.get("/logs", params={"outcome": "VICTORY"}).text
    assert _link(WON) in won
    for dropped in (LOST, FLYING, REJECTED, NEVER_LEFT):
        assert _link(dropped) not in won

    lost = client.get("/logs", params={"outcome": "FAIL"}).text
    assert _link(LOST) in lost
    assert _link(WON) not in lost

    awaiting = client.get("/logs", params={"outcome": "AWAITING"}).text
    for kept in (FLYING, REJECTED, NEVER_LEFT):
        assert _link(kept) in awaiting
    for dropped in (WON, LOST):
        assert _link(dropped) not in awaiting


# ---- 叠加 -----------------------------------------------------------------


def test_the_three_quick_filters_compose_with_each_other(tmp_path: Path) -> None:
    """一起用是 AND，不是谁覆盖谁。"""
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"preset": "AAA", "result": "SENT", "outcome": "FAIL"}).text

    assert _link(LOST) in body
    for dropped in (WON, FLYING, REJECTED, NEVER_LEFT):
        assert _link(dropped) not in body


def test_the_quick_filters_compose_with_the_existing_ones(tmp_path: Path) -> None:
    """事件类型 / 日期 / 目标坐标那三个也要能和新的叠加。"""
    _, client = _seed(tmp_path)

    body = client.get(
        "/logs",
        params={
            "kind": TARGET_KIND_PIRATE,
            "date": "2026-08-09",
            "target_start": "2:137:1",
            "target_end": "2:137:3",
            "preset": "AAA",
            "outcome": "VICTORY",
        },
    ).text

    assert _link(WON) in body
    for dropped in (LOST, FLYING, REJECTED, NEVER_LEFT):
        assert _link(dropped) not in body


def test_switching_event_kind_carries_the_quick_filters_along(tmp_path: Path) -> None:
    """切换任一档都不该把其余的甩掉，否则每种视图都没有可分享的链接。"""
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"preset": "AAA", "result": "SENT"}).text

    assert "preset=AAA" in body
    assert "result=SENT" in body
    # 另外两张表单也得把这三个值带上，否则一提交就互相清空。
    assert '<input type="hidden" name="preset" value="AAA">' in body
    assert '<input type="hidden" name="result" value="SENT">' in body


# ---- 筛选真的下推了 SQL ---------------------------------------------------


def test_the_preset_filter_reaches_past_the_row_limit(tmp_path: Path) -> None:
    """**这条是这份文件的重点。**

    `ZZZ-旧预设` 那一发排在 `ATTACK_LOG_LIMIT` 条填充记录之后。在内存里筛的话
    它永远取不到，页面会显示「还没有攻击记录」——而那读起来就是「这个预设一发
    没打过」。只有把 `preset` 下推到 SQL，它才回得来。
    """
    _, client = _seed(tmp_path)

    body = client.get("/logs", params={"preset": OLD_PRESET.name}).text

    assert _link(OLD) in body
    assert "还没有攻击记录" not in body


def test_the_result_and_outcome_filters_also_reach_past_the_row_limit(tmp_path: Path) -> None:
    """同一个坑，三档一起验：服务层拿 limit=1 也必须能翻出那条老记录。"""
    service, _ = _seed(tmp_path)

    assert [e.target for e in service.list_attack_log(1, preset=OLD_PRESET.name)] == [OLD]
    assert [
        e.target for e in service.list_attack_log(1, preset=OLD_PRESET.name, result="SENT")
    ] == [OLD]
    assert [
        e.target for e in service.list_attack_log(1, preset=OLD_PRESET.name, outcome="VICTORY")
    ] == [OLD]


# ---- 候选值从库里取 -------------------------------------------------------


def test_the_preset_options_come_from_the_database(tmp_path: Path) -> None:
    """预设是用户自己在游戏里维护的，写死字面量就会漏掉他新建的那一个。"""
    service, client = _seed(tmp_path)

    options = service.attack_log_options()

    assert set(options.presets) == {"AAA", "BBB", OLD_PRESET.name, "填充"}
    body = client.get("/logs").text
    for name in ("AAA", "BBB", OLD_PRESET.name):
        assert f'<option value="{name}"' in body


def test_the_outcome_options_come_from_the_database_and_read_in_chinese(tmp_path: Path) -> None:
    """战果同理：库里存的是画面原文，将来多一档也得能筛。"""
    service, client = _seed(tmp_path)

    options = service.attack_log_options()

    # 顺序由 `_ordered_outcomes` 定：胜、负、待战报。字母序会把 AWAITING 排到最前。
    assert set(options.outcomes) == {"VICTORY", "FAIL", "AWAITING"}
    body = client.get("/logs").text
    assert body.index('value="VICTORY"') < body.index('value="FAIL"')
    assert body.index('value="FAIL"') < body.index('value="AWAITING"')


def test_an_unknown_quick_filter_value_does_not_return_an_error_page(tmp_path: Path) -> None:
    """手改链接写错一个档不该换来一页 JSON——那读起来就是「控制台坏了」。"""
    _, client = _seed(tmp_path)

    response = client.get("/logs", params={"result": "NEVER"})

    assert response.status_code == 200
    # `NEVER` 在这一页不存在（一行就是一次派遣意图），当成没筛。
    assert "未按预设 / 结果 / 战果筛选" in response.text


# ---- 事件类型的配色 -------------------------------------------------------


def test_bot_and_pirate_rows_render_different_kind_chips(tmp_path: Path) -> None:
    """bot 与海盗在「事件类型」那一列要一眼分得开，且复用情报中心那一套样式。

    两个 class 都不能为空、且必须不同：同一种灰 chip 正是这次要修的问题。
    色永远配一个字形和一个词，所以「bot」「海盗」两个词也一并钉住。
    """
    _, client = _seed(tmp_path)

    body = client.get("/logs").text

    assert 'class="chip kind-bot"' in body
    assert 'class="chip kind-pirate"' in body
    assert "▣" in body
    assert "☠" in body
    assert "海盗" in body


def test_the_kind_chip_styles_are_the_ones_intel_already_uses(tmp_path: Path) -> None:
    """两页对同一个概念用两种色，比两页都不上色更糟。"""
    from evo_helper.web.display import TARGET_KIND_GLYPHS, TARGET_KIND_TONES

    _, client = _seed(tmp_path)
    body = client.get("/logs").text

    for kind, tone in TARGET_KIND_TONES.items():
        assert tone, kind
        assert f'class="chip {tone}"' in body, kind
        assert TARGET_KIND_GLYPHS[kind] in body, kind

    # 借 ok / warn / danger 会让人以为海盗行出了问题——bot 与海盗之间没有好坏。
    assert set(TARGET_KIND_TONES.values()).isdisjoint({"ok", "warn", "danger"})
