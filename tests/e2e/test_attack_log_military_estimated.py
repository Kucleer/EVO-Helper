"""攻击日志的「目标军力」那一格：插出来的数要当场标「(估算)」。

用户口径（2026-08-18）：「`military_score_estimated` 要加，单独 PR」「用 A」——
A 方案就是**估算平铺、时刻进角标**：`72,252 (估算) ⓘ`。

## 这个标记的含义不是「大概齐」

为真时说的是：这一行从榜上读到的分数**破坏了降序、被判为不可信丢掉了**，
显示出来的数是拿上下两个好邻居**插出来的中点**——压根不是读到的值。来历是
2026-08-15 的事故（`tools/ranking_scan.py` 的注释里有完整记述）：30 个 bot 的
军力飞到 10 万以上，每一个除以 100 都精确落回正常区间，`17.73K` 读成 `1773K`。

**规模不小**：2026-08-18 生产库里 3225 个有读数的 bot 中，估算的有 365 个
（11.3%）。不标出来就是每九行里有一行把插值当实读展示。

## 为什么必须快照，不能事后现取

`bot_targets.military_score_estimated` 会被反复重写和清零——每轮采集整行覆盖、
`clear_pirate_position_bot_candidates` 清成 `False`、`forget_implausible_military_scores`
清成 `False`（2026-08-18 跑过两次）。事后现取，今天标着「估算」的记录明天会
自己变成「实读」。第一节钉的就是这件事。

## 三档，不是两档

`True` 标「(估算)」、`False` 什么都不标、`None` 也什么都不标——但两种「不标」
含义相反：`False` 是「这个数是实读的」，`None` 是「2026-08-18 之前派的，当时
没记这件事」。第三节钉住 `None` 既不被回填成 `False`、页面也不替它声称实读。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate, FleetPresetRef
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
    AttackDispatch,
    AttackIntent,
    RankingTarget,
)
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from evo_helper.web.service import ScanRangeView
from support.database import scratch_database_url
from support.runs import seed_run_instance

ORIGIN = Coordinate(2, 137, 18)
#: 9 号位：1--4 号是游戏固定生成的海盗，进不了 `bot_targets`（`is_bot_coordinate`）。
BOT_TARGET = Coordinate(2, 137, 9)
#: 4 号位是海盗，`bot_targets` 里没有它的行——「连读数都没有」的那一档。
PIRATE_TARGET = Coordinate(2, 137, 4)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
DISPATCHED = datetime(2026, 8, 18, 3, 55, tzinfo=UTC)
OBSERVED = DISPATCHED - timedelta(hours=3)
PRESET = FleetPresetRef(name="BBB", signature="深空吞噬者:70")

#: 生产实测的那一条：`4:336:11 = 72,252（估算）`，2026-08-18 的清理把它的
#: `estimated` 从 `True` 清成了 `False`——正是「事后现取会改口」的实证。
ESTIMATED_SCORE = 72252.0

#: 页面上那个标记的原文。**照抄军力榜**（`rankings.html` 里
#: `${r.estimated ? ' <span class="muted">(估算)</span>' : ''}`）：同一个概念在
#: 两页上写成两种样子，比两页都不标更让人犯迷糊。
ESTIMATED_MARK = '<span class="muted">(估算)</span>'

#: 读数时刻那个角标的类名（#183 加的）。按类名认，不按字符认。
TIP_CLASS = 'class="score-tip"'


def _factory(tmp_path: Path) -> tuple[SqlAlchemyRepository, UUID, sessionmaker[Session]]:
    engine = create_database_engine(scratch_database_url(tmp_path, "estimated-mark.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    service = PersistentApplicationService(factory, now_utc=lambda: DISPATCHED)
    plan = service.create_plan(
        name="bot 攻击",
        enabled=True,
        window_start=time(8),
        window_end=time(20),
        ranges=(
            ScanRangeView(
                Coordinate(2, 137, 1), BOT_TARGET, ORIGIN, PRESET.name, PRESET.signature, 0
            ),
        ),
    )
    run_id = seed_run_instance(
        factory, plan_id=plan.id, idempotency_key="estimated-0001", created_at_utc=DISPATCHED
    )
    return SqlAlchemyRepository(factory), run_id, factory


def _seed_score(
    repository: SqlAlchemyRepository,
    score: float,
    observed_at: datetime,
    *,
    estimated: bool,
) -> None:
    repository.save_ranking_targets(
        [
            RankingTarget(
                coordinate=BOT_TARGET,
                military_score=score,
                military_score_at_utc=observed_at,
                military_score_estimated=estimated,
                military_rank=7,
            )
        ]
    )


def _dispatch(
    repository: SqlAlchemyRepository,
    run_id: UUID,
    target: Coordinate,
    *,
    kind: str = TARGET_KIND_BOT,
) -> UUID:
    intent = AttackIntent(
        intent_id=uuid4(),
        run_id=run_id,
        origin=ORIGIN,
        target=target,
        preset=PRESET,
        cycle_start_utc=CYCLE,
        created_at_utc=DISPATCHED - timedelta(minutes=1),
        target_kind=kind,
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
    return intent.intent_id


#: 「目标军力」是表头里的第 5 列（1 起数），紧跟在「目标」后面。
SCORE_COLUMN = 5


def _score_cell(html: str) -> str:
    """把表格体第一行里「目标军力」那一格的原样 HTML 取出来。

    ⚠️ **只搜这一格，绝不整页搜。** 表格上方那句说明里也写着 `(估算)`（它必须
    在那儿：这两个字不解释一遍，读者最自然的理解恰恰是「差不多准」）。整页搜
    「有没有标估算」的话，无论哪一行是什么样，那一条都永远绿。
    """
    start = html.find("<tbody")
    assert start != -1, "页面上没有表格体，这几条用例的前提就不成立"
    row = re.search(r"<tr[^>]*>(.*?)</tr>", html[start:], re.DOTALL)
    assert row is not None, "表格体里一行都没有，这几条用例的前提就不成立"
    cells = row.group(1).split("<td")
    assert len(cells) > SCORE_COLUMN, f"这一行只有 {len(cells) - 1} 格，取不到「目标军力」那一列"
    return cells[SCORE_COLUMN]


def _logs(factory: sessionmaker[Session]) -> str:
    return TestClient(create_persistent_app(factory)).get("/logs").text


# -- 一、事后重扫改了 estimated，日志那一格不跟着变 ------------------------------


def test_a_later_rescan_cannot_unmark_an_estimated_reading(tmp_path: Path) -> None:
    """**这一条是整个需求的判据。**

    先在标着「估算」的时候派一发，再让军力榜把 `bot_targets` 那一行整个刷成实读。
    日志上那一格必须**仍然**标着「(估算)」——不标就说明这一列连的是现值，而现值
    答的是「它现在是不是估算」，不是「派这一发时我看到的是个插出来的数」。

    这不是假想：生产里 `4:336:11 = 72,252（估算）` 在 2026-08-18 的清理之后
    `estimated` 就变成了 `False`。事后现取的版本不会报错，只会安静地改口。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)
    _dispatch(repository, run_id, BOT_TARGET)

    # 军力榜又采了一轮，同一个坐标的整行被覆盖成实读。
    _seed_score(repository, ESTIMATED_SCORE, DISPATCHED + timedelta(hours=6), estimated=False)

    cell = _score_cell(_logs(factory))

    assert ESTIMATED_MARK in cell, "重扫把日志上的「(估算)」抹掉了——这一格被连成了现值"
    assert "72,252" in cell


def test_clearing_the_flag_in_bot_targets_leaves_the_log_alone(tmp_path: Path) -> None:
    """另一条会清零的链路：`forget_implausible_military_scores`。

    它把高于阈值的读数连同 `military_score_estimated` 一起归零位（2026-08-18
    实机跑过两次）。这条走的是和上面那条不同的代码路径——一个是 upsert 覆盖，
    一个是显式清空——但对日志的要求一样：**已经写下的那一格不许被追改**。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)
    _dispatch(repository, run_id, BOT_TARGET)

    cleared = repository.forget_implausible_military_scores(above=50_000)

    assert cleared == 1, "这一条的前提是那一行真的被清了，没清就什么都没验到"
    cell = _score_cell(_logs(factory))
    assert ESTIMATED_MARK in cell, "清空 `bot_targets` 的读数把日志上的「(估算)」也带走了"


def test_the_snapshot_column_holds_the_flag_from_the_moment_of_dispatch(tmp_path: Path) -> None:
    """库这一层：标记和分数是同一条 SELECT、同一个事务里抄下来的。

    页面那两条是从结果反推的；这一条直接看快照列，省得「页面对了但库里没存」
    这种情形靠渲染顺序蒙混过去。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)

    intent_id = _dispatch(repository, run_id, BOT_TARGET)

    with factory() as session:
        row = session.scalar(select(orm.AttackIntentRow).where(orm.AttackIntentRow.id == intent_id))
    assert row is not None
    assert row.target_military_score_estimated is True
    # 三列是一起写的：只抄了标记没抄数，标记就没有依附的对象。
    assert row.target_military_score == ESTIMATED_SCORE
    assert row.target_military_score_at_utc == OBSERVED


# -- 二、True 出现「(估算)」，False 不出现 ---------------------------------------


def test_an_estimated_reading_is_marked_in_place(tmp_path: Path) -> None:
    """`True`：分数后面平铺一个「(估算)」，和读数时刻那个角标并存。

    ⚠️ **不许藏进角标。** 用户选 A 方案的理由就是这个：估算是要**一眼看见**的
    警示（它说的是「这个数不是读到的」），而快照时刻是查的时候才要的一句。
    藏进 hover 等于默认用户不会去看。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(_logs(factory))

    assert ESTIMATED_MARK in cell, "插出来的数没有标「(估算)」，页面在把它当实读展示"
    assert "72,252" in cell
    # 标记在数后面、角标之前：`72,252 (估算) ⓘ`。
    assert cell.index("72,252") < cell.index(ESTIMATED_MARK) < cell.index(TIP_CLASS)


def test_a_real_reading_carries_no_mark(tmp_path: Path) -> None:
    """`False`：什么都不标。

    这一条和上面那条是一对。只钉「True 时出现」的话，把标记无条件贴在每一行上
    也照样绿——而那种页面等于没有标记，读者分不出哪几行是插出来的。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=False)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(_logs(factory))

    assert "72,252" in cell
    assert "(估算)" not in cell, "实读的分数被标成了估算"


# -- 三、历史行是 NULL：既不标估算，也不声称实读 ---------------------------------


def test_a_row_from_before_this_column_existed_is_not_backfilled(tmp_path: Path) -> None:
    """快照列为 NULL 的历史行**不许被回填成 `False`**。

    `False` 在这一列上的含义是「这个数是实读的」。2026-08-18 之前派出去的那些
    发次根本不知道当时那个数是怎么来的——默认成 `False` 就是让它们冒充实读，
    而这个标记存在的全部意义正是把插值和实读分开。

    构造方式是照实模拟：意图写下去的时候库里还没有这个坐标的军力行（于是三列
    一起是 NULL），事后军力榜才第一次采到它，而且采到的是个估算值。
    """
    repository, run_id, factory = _factory(tmp_path)
    intent_id = _dispatch(repository, run_id, BOT_TARGET)

    _seed_score(repository, ESTIMATED_SCORE, DISPATCHED + timedelta(hours=6), estimated=True)

    with factory() as session:
        row = session.scalar(select(orm.AttackIntentRow).where(orm.AttackIntentRow.id == intent_id))
    assert row is not None
    assert row.target_military_score_estimated is None, (
        "没有观测的行被写成了 `False`——那是在声称「当时读到的是实读值」"
    )
    cell = _score_cell(_logs(factory))
    # 页面上两边都不说：既不标「(估算)」，也没有一个反过来声称实读的标记。
    assert "(估算)" not in cell, "历史行被标成了估算"
    assert "实读" not in cell, "历史行被反过来声称成实读"
    assert "—" in cell, "历史行的军力该是「—」"


def test_a_target_without_any_reading_snapshots_null_not_false(tmp_path: Path) -> None:
    """海盗位在 `bot_targets` 里根本没有行，标记同样是 NULL。

    没有分数就谈不上「这个分数是插出来的还是读到的」。写 `False` 会让库里多出
    一批「实读」记录，而它们连一个数都没有。
    """
    repository, run_id, factory = _factory(tmp_path)

    intent_id = _dispatch(repository, run_id, PIRATE_TARGET, kind=TARGET_KIND_PIRATE)

    with factory() as session:
        row = session.scalar(select(orm.AttackIntentRow).where(orm.AttackIntentRow.id == intent_id))
    assert row is not None
    assert row.target_military_score is None
    assert row.target_military_score_estimated is None


# -- 四、#183 的角标不受影响 -----------------------------------------------------


def test_the_hover_badge_still_shows_up_next_to_the_mark(tmp_path: Path) -> None:
    """有快照的行照旧出角标，读数时刻仍在它的 `title` 里。

    这一列是挤在 #183 刚做好的那一格里的。标记加进去而把角标挤掉（或者把时刻
    从 `title` 里挤出来平铺成第二行），就是把上一个 PR 的成果撞回去了。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(_logs(factory))

    assert TIP_CLASS in cell, "加了「(估算)」之后角标不见了"
    assert 'title="派出时的读数：2026-08-18 08:55:00（现实 UTC+8）"' in cell
    # 整格仍旧只有一行：时刻没有被挤成平铺的第二行。
    assert "<div" not in cell


def test_a_history_row_still_has_no_badge(tmp_path: Path) -> None:
    """历史行仍旧一个角标都没有——加了这一列也不该凭空长出一个空 tooltip。"""
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(_logs(factory))

    assert TIP_CLASS not in cell, "没有快照的行长出了角标——点开是一片空白"
    assert "派出时的读数" not in cell


# -- 五、页面得解释这两个字 ------------------------------------------------------


def test_the_page_says_what_estimated_means(tmp_path: Path) -> None:
    """「(估算)」不解释一遍，读者会理解成「大概齐」——而实情是它压根不是读到的。

    说明必须落在页面上，不能只写在代码注释里：看这一页的人不看代码。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)
    _dispatch(repository, run_id, BOT_TARGET)

    html = _logs(factory)

    assert "(估算)" in html
    assert "不是实读" in html, "页面没说清「(估算)」意味着这个数不是读到的"
