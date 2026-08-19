"""攻击日志的「出发星球」筛选，以及压扁之后的筛选栏。

用户口径（2026-08-19）：「攻击日志增加出发星球快速筛选，优化筛选栏分布，
不要占太多高度」。

## 候选值从哪儿来：两张表并起来，缺一不可

⚠️ **这不是想出来的，是生产库只读实测**（2026-08-19）：

- `mission_task_origins` 里配着两颗；
- `attack_intents` 里真正出现过的出发点是**三个**——占日志七成的那一个从来
  没进过 `mission_task_origins`。

于是两种偷懒各有一种失败：

- 只取配置表：占七成的那个出发点在下拉框里根本不存在，用户只会觉得筛选坏了。
  页面上看不出少了它——**下拉框里没有的那一档，谁都不会知道它本来该在**。
- 只取日志：用户新加一颗星球、还没派出第一发之前，那颗不在候选里。而「随时会加」
  正是这个筛选要跟上的事。

第一节钉两边各自不许掉，第二节钉「一个坐标都不许写死」。

## 筛选栏压高度

原先三张 `filter-bar` 各占一行，右边各挂一句「未按 X 筛选」。现在**七个筛选一张
表单**，底下并成一句状态。

⚠️ **压高度不许靠删功能。** 第三节逐个点名七个筛选维度都还在，第四节钉住那句状态
把没筛的维度**逐个**点出来——它回答的是「我现在看到的是不是全集」，笼统一句
「其余未筛」答不了「今天到底有没有按出发星球筛」这种问题。

## 坐标一律是编出来的

这个仓是公开的。这份文件里的坐标与生产无关，别拿它们对照实机。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import TARGET_KIND_BOT, AttackDispatch, AttackIntent
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import ATTACK_LOG_LIMIT, create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.database import scratch_database_url
from support.runs import seed_run_instance

NOW = datetime(2026, 8, 18, 3, 55, tzinfo=UTC)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
PRESET = FleetPresetRef(name="AAA", signature="预设:AAA")

#: 日志里出现过、但**没有**配在 `mission_task_origins` 里的那个出发点。
#: 生产上正是这一档占了七成，只读配置表就会把它整个漏掉。
LOGGED_ONLY = Coordinate(5, 311, 12)
#: 既配着、日志里也有的那个。
CONFIGURED = Coordinate(6, 404, 3)
#: 刚配上、还一发没打的那颗——「用户随时会加」说的就是它。
FRESHLY_ADDED = Coordinate(7, 512, 9)

TARGET_A = Coordinate(5, 311, 7)
TARGET_B = Coordinate(6, 404, 8)

#: 七个筛选维度。少一个就是压高度压到把功能压没了。
DIMENSIONS = ("事件类型", "出发星球", "预设", "结果", "战果", "日期", "目标坐标")


def _factory(tmp_path: Path) -> tuple[SqlAlchemyRepository, UUID, sessionmaker[Session]]:
    engine = create_database_engine(scratch_database_url(tmp_path, "origin-filter.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: NOW)
    plan = service.create_plan(
        name="bot 攻击",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        ranges=(ScanRangeView(TARGET_A, TARGET_B, LOGGED_ONLY, PRESET.name, PRESET.signature, 0),),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="origin-filter-1", created_at_utc=NOW
    )
    return SqlAlchemyRepository(factory), run_id, factory


def _dispatch(
    repository: SqlAlchemyRepository,
    run_id: UUID,
    origin: Coordinate,
    target: Coordinate,
    *,
    at: datetime,
    cycle: datetime = CYCLE,
) -> None:
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=origin,
        target=target,
        preset=PRESET,
        cycle_start_utc=cycle,
        created_at_utc=at - timedelta(minutes=1),
        target_kind=TARGET_KIND_BOT,
    )
    repository.save_attack_intent(intent)
    repository.save_dispatch(
        AttackDispatch(
            dispatch_id=uuid4(),
            intent_id=intent.intent_id,
            dispatched_at_utc=at,
            accepted=True,
        )
    )


def _configure_origin(repository: SqlAlchemyRepository, *planets: Coordinate) -> None:
    """把几颗星球配进 `mission_task_origins`（走真实入口，不直接塞行）。"""
    repository.ensure_mission_rows(now_utc=NOW)
    task = next(row for row in repository.mission_tasks() if row.kind == MissionKind.BOT.value)
    rows = [(repository.create_attack_planet(planet).id, 5, True) for planet in planets]
    repository.replace_mission_task_origins(task.id, rows)


def _client(factory: sessionmaker[Session]) -> TestClient:
    return TestClient(create_persistent_app(factory))


def _origin_select(html: str) -> str:
    """出发星球那个下拉框的原样 HTML。

    ⚠️ **只搜这一个下拉框。** 坐标同时出现在表格的「出发」那一列里，整页搜
    「候选里有没有它」的话，把整个下拉框删光也照样绿。
    """
    match = re.search(r'<select id="log-origin".*?</select>', html, re.DOTALL)
    assert match is not None, "页面上没有出发星球下拉框"
    return match.group(0)


def _text(value: Coordinate) -> str:
    return f"{value.galaxy}:{value.system}:{value.position}"


# -- 一、候选值：两张表并起来，各自都不许掉 --------------------------------------


def test_an_origin_that_only_appears_in_the_log_is_still_offered(tmp_path: Path) -> None:
    """⚠️ **只读 `mission_task_origins` 的版本在这一条上红。**

    生产上占日志七成的那个出发点从来没进过配置表。它不在候选里的话，那七成记录
    就再也筛不出来——而页面上一点异样都没有，用户只会觉得筛选坏了。
    """
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW)
    _configure_origin(repository, CONFIGURED)

    options = _origin_select(_client(factory).get("/logs").text)

    assert _text(LOGGED_ONLY) in options, "日志里出现过的出发点不在候选里，那些记录筛不出来"


def test_a_freshly_configured_origin_is_offered_before_its_first_dispatch(
    tmp_path: Path,
) -> None:
    """⚠️ **这一条就是「不许写死」那句要求本身。**

    候选值真从 `mission_task_origins` 现取的话，用户刚配上的那颗**立刻**出现在
    下拉框里，哪怕它一发还没打。写死一份名单（或者只数日志里出现过的）在这一条上红。

    代价是这一档会筛出 0 行。**这里接受它**：那 0 行本身就是答案——配了，一发还
    没打出去。他配了却在筛选里找不到，比筛出来是空更让人怀疑控制台。
    """
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW)

    before = _origin_select(_client(factory).get("/logs").text)
    assert _text(FRESHLY_ADDED) not in before, "这条用例的前提是它一开始不在候选里"

    _configure_origin(repository, CONFIGURED, FRESHLY_ADDED)

    after = _origin_select(_client(factory).get("/logs").text)
    assert _text(FRESHLY_ADDED) in after, "新配上的出发星球没进候选——名单是写死的"
    assert _text(CONFIGURED) in after
    # 日志那一侧的那个没有因此被挤掉：两张表是并起来，不是二选一。
    assert _text(LOGGED_ONLY) in after


def test_the_options_are_listed_in_coordinate_order(tmp_path: Path) -> None:
    """按坐标排序，不按「谁先出现在哪张表里」。

    两张表并起来的顺序是实现细节；用户找一颗星球是按坐标找的。
    """
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, CONFIGURED, TARGET_B, at=NOW)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW - timedelta(minutes=5))
    _configure_origin(repository, FRESHLY_ADDED)

    options = _origin_select(_client(factory).get("/logs").text)
    order = [options.index(_text(item)) for item in (LOGGED_ONLY, CONFIGURED, FRESHLY_ADDED)]

    assert order == sorted(order), "候选值没按坐标排序"


def test_each_origin_is_offered_only_once(tmp_path: Path) -> None:
    """两张表都有的那一颗只出现一次——并集不是拼接。"""
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, CONFIGURED, TARGET_B, at=NOW)
    _configure_origin(repository, CONFIGURED)

    options = _origin_select(_client(factory).get("/logs").text)

    assert options.count(f'value="{_text(CONFIGURED)}"') == 1, "同一颗星球在候选里出现了两遍"


# -- 二、真的筛了，而且下推到 SQL -------------------------------------------------


def test_filtering_by_origin_keeps_only_that_origin(tmp_path: Path) -> None:
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW)
    _dispatch(repository, run_id, CONFIGURED, TARGET_B, at=NOW - timedelta(minutes=5))

    body = _client(factory).get("/logs", params={"origin": _text(LOGGED_ONLY)}).text
    rows = body[body.find("<tbody") :]

    assert _text(TARGET_A) in rows
    assert _text(TARGET_B) not in rows, "别的出发星球那一发没被筛掉"


def test_the_origin_filter_reaches_past_the_row_limit(tmp_path: Path) -> None:
    """⚠️ **筛选必须下推到 SQL。**

    这一页只取最近 `ATTACK_LOG_LIMIT` 条。在内存里筛出发星球等于「先砍掉历史再问
    历史」——查一颗很久没用的星球必得空页，而空页读起来就是「那颗星球一发没打过」。
    这一页的日期、坐标、事件类型都各踩过一次这个坑。
    """
    repository, run_id, factory = _factory(tmp_path)
    # 最老的那一发用 LOGGED_ONLY，然后拿别的星球把它挤到 limit 之外。
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW - timedelta(days=9))
    # 每一发换一个轮次：同一个 run/target/cycle 只许有一条意图（唯一约束）。
    for index in range(ATTACK_LOG_LIMIT + 5):
        _dispatch(
            repository,
            run_id,
            CONFIGURED,
            TARGET_B,
            at=NOW - timedelta(minutes=index),
            cycle=CYCLE + timedelta(hours=index),
        )

    body = _client(factory).get("/logs", params={"origin": _text(LOGGED_ONLY)}).text

    assert _text(TARGET_A) in body[body.find("<tbody") :], "被 limit 挡掉了——这一档是在内存里筛的"


def test_an_unknown_origin_is_treated_as_no_filter(tmp_path: Path) -> None:
    """手改链接写一个不存在的坐标不该换来一页 JSON，也不该换来一页空白。

    ⚠️ 判据是**按候选表核**，不是「解析得动就用」：`9:999:9` 解析得动却一行都筛
    不出来，而空页读起来就是「那颗星球一发没打过」。当成没筛，并在状态那句话里说
    清楚这一页没按出发星球筛。
    """
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW)

    response = _client(factory).get("/logs", params={"origin": "9:999:9"})

    assert response.status_code == 200
    assert _text(TARGET_A) in response.text[response.text.find("<tbody") :]
    assert "出发星球" in _summary(response.text), "没说清这一页没按出发星球筛"
    assert "未按 " in _summary(response.text)


def test_an_empty_origin_is_not_a_422(tmp_path: Path) -> None:
    """下拉框「全部」那一项 value 是空串，提交表单必然带上 `origin=`。"""
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW)

    response = _client(factory).get("/logs", params={"origin": "", "preset": "", "result": ""})

    assert response.status_code == 200
    assert _text(TARGET_A) in response.text[response.text.find("<tbody") :]


# -- 三、筛选栏压扁了，但一个维度都没少 ------------------------------------------


def _summary(html: str) -> str:
    match = re.search(r'<p class="filter-summary muted">(.*?)</p>', html, re.DOTALL)
    assert match is not None, "筛选栏底下那句状态不见了——用户看不出自己看到的是不是全集"
    return match.group(1)


def test_the_whole_filter_bar_is_a_single_form(tmp_path: Path) -> None:
    """三行合成一行的前提：**一张表单**。

    三张表单时代，每一张都得把另外两张的值抄成 `<input type="hidden">`，
    否则提交任何一张就把另外两张清空。合成一张之后那些抄件必须全部消失——
    留着的话说明表单其实没合并，只是把三行叠得近了一点。
    """
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW)

    body = _client(factory).get("/logs").text

    assert body.count('<form class="filter-bar"') == 1, "筛选栏还是不止一张表单"
    assert '<input type="hidden"' not in body, "还在抄隐藏字段——表单没有真的合并"


def test_no_filter_dimension_was_dropped_while_shrinking_the_bar(tmp_path: Path) -> None:
    """⚠️ **压高度不许靠删功能。**

    七个维度各自的控件都得还在（按 `name` 认，不按标签文字认——标签可以改措辞，
    控件才是那个筛选本身）。
    """
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW)

    body = _client(factory).get("/logs").text

    for field in ("kind", "origin", "preset", "result", "outcome", "date", "target_start"):
        assert f'name="{field}"' in body, f"「{field}」这一档被压没了"
    assert 'name="target_end"' in body, "坐标区间只剩一端"


# -- 四、那句状态：七个维度一个不许掉 --------------------------------------------


def test_the_summary_names_every_unfiltered_dimension(tmp_path: Path) -> None:
    """⚠️ **这三句「未按 X 筛选」合成一句，但内容一个字都不许少。**

    它回答的是「我现在看到的是不是全集」。少了哪一档，用户就会把筛过的一页当成
    全部，而页面上一点异样都没有——2026-08-19 压高度时最容易顺手砍掉的就是它。
    """
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW)

    summary = _summary(_client(factory).get("/logs").text)

    assert "未按" in summary
    for dimension in DIMENSIONS:
        assert dimension in summary, f"一档都没筛，那句话却没点名「{dimension}」"


def test_the_summary_separates_what_is_filtered_from_what_is_not(tmp_path: Path) -> None:
    """筛了几档时，那句话既说「只看什么」，也说「其余哪几档没筛」。

    只说前半句是最坏的一种：用户看见「只看 预设 AAA」，会以为其余都已经限定过了。
    """
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW)
    _configure_origin(repository, CONFIGURED)

    summary = _summary(
        _client(factory).get("/logs", params={"preset": "AAA", "origin": _text(LOGGED_ONLY)}).text
    )

    assert "只看" in summary
    assert "预设 AAA" in summary
    assert f"出发星球 {_text(LOGGED_ONLY)}" in summary
    assert "未按" in summary
    for dimension in ("事件类型", "结果", "战果", "日期", "目标坐标"):
        assert dimension in summary, f"没筛的「{dimension}」没被点名"


def test_the_summary_says_the_page_is_capped(tmp_path: Path) -> None:
    """「这一页最多 N 条」也是「是不是全集」的一部分：筛选全空也未必是全部历史。"""
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, LOGGED_ONLY, TARGET_A, at=NOW)

    summary = _summary(_client(factory).get("/logs").text)

    assert str(ATTACK_LOG_LIMIT) in summary, "那句话没说这一页有取数上限"


def test_the_filter_bar_wraps_instead_of_overflowing() -> None:
    """七个筛选挤在一行上，**窄屏必须折行而不是溢出**。

    这一页为宽度收拾过两轮（#178 限宽、#183 加列），横向滚动条是用户报过的老问题。
    `flex-wrap: wrap` 是这里唯一的保险：放得下就是一行，放不下自己变回两行。
    """
    css = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "evo_helper"
        / "web"
        / "static"
        / "console.css"
    ).read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    rule = re.search(r"\.filter-bar\s*\{([^}]*)\}", css)

    assert rule is not None, "样式表里没有 `.filter-bar`"
    assert "flex-wrap: wrap" in rule.group(1), "筛选栏不折行了——窄屏会拖出横向滚动条"
