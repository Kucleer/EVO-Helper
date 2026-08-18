"""攻击日志的「目标军力」列：显示的是**派那一发时**看到的读数。

用户口径（2026-08-18）：「攻击日志页面增加目标军力列，取值他判断发动攻击时拿到
的军力」。

## 为什么这一列不能连 `bot_targets` 现取

`bot_targets.military_score` 是当前值，每采一次军力榜就整行覆盖——生产实测
（2026-08-18）同一批目标一天之内从 31,756 一路刷到 2,616。事后 join 现值，
日志答的是「它现在多强」；而这一列存在的全部意义是回答「当时凭什么打它」。
两者在复盘时恰好相反，而且现取那一版不会报错，只会安静地逐渐改口。

所以值在写意图的同一个事务里从 `bot_targets` 抄一份进 `attack_intents`
（`storage.repository.save_attack_intent`），此后再不跟着变。下面第一节钉的就是
这件事：**先派一发，再把 `bot_targets` 改掉，日志那一格不许跟着动。**

## 另外两条同样是「别拿一个数糊弄过去」

- 没有军力读数的目标（海盗位在 `bot_targets` 里根本没有行）显示「—」，**不是 0**。
  被打空的 bot 军力真的是 0，两者在这一页上必须分得开。
- 这一列 2026-08-18 才开始记，之前的意图快照是 NULL。**照实显示「—」，不许拿
  现值回填**——补一个看起来合理的数进去，就再也分不出哪几行是真的读到过。

## 读数时刻藏在角标后面

用户口径（2026-08-18）：「增加军力值的内容，还需要一个 tips 角标，hover 效果是
当时快照的时间」。所以时刻不再平铺成格子里的第二行，改成军力值后面一个 `ⓘ`，
时刻放在它的原生 `title` 上。第四节钉的是这件事的两半：**有快照才有角标**，
以及**角标里真的写着那个时刻**。
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
#: 5 号位：1--4 号是游戏固定生成的海盗，进不了 `bot_targets`（`is_bot_coordinate`）。
BOT_TARGET = Coordinate(2, 137, 9)
#: 4 号位是海盗，`bot_targets` 里没有它的行——「没有军力读数」的那一档。
PIRATE_TARGET = Coordinate(2, 137, 4)
CYCLE = datetime(2026, 8, 3, tzinfo=UTC)
DISPATCHED = datetime(2026, 8, 18, 3, 55, tzinfo=UTC)
#: 读数比派遣早三小时——「这个分数是什么时候读的」正是要显示出来的那半句。
OBSERVED = DISPATCHED - timedelta(hours=3)
PRESET = FleetPresetRef(name="BBB", signature="深空吞噬者:70")

#: 派遣那一刻榜上的值，和事后被扫描覆盖成的值。两个数取自生产实测的那一对。
SCORE_WHEN_DISPATCHED = 31756.0
SCORE_AFTER_RESCAN = 2616.0


def _factory(tmp_path: Path) -> tuple[SqlAlchemyRepository, UUID, sessionmaker[Session]]:
    engine = create_database_engine(scratch_database_url(tmp_path, "target-military.db"))
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
        factory, plan_id=plan.id, idempotency_key="score-log-0001", created_at_utc=DISPATCHED
    )
    return SqlAlchemyRepository(factory), run_id, factory


def _seed_score(repository: SqlAlchemyRepository, score: float, observed_at: datetime) -> None:
    repository.save_ranking_targets(
        [
            RankingTarget(
                coordinate=BOT_TARGET,
                military_score=score,
                military_score_at_utc=observed_at,
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

#: 读数时刻那个角标的类名。**按类名认，不按字符认**——角标用哪个字符是版面选择
#: （`ⓘ` / `⏱` 都行），而「有没有这个角标」是判据，两者不该绑在一起。
TIP_CLASS = 'class="score-tip"'


def _score_cell(html: str) -> str:
    """把表格体第一行里「目标军力」那一格的原样 HTML 取出来。

    按列的位置取，不按类名取——理由同 `test_attack_log_width._outcome_cell`：
    整页搜会命中顶上那句「自 2026-08-18 起记录」的说明。
    """
    start = html.find("<tbody")
    assert start != -1, "页面上没有表格体，这几条用例的前提就不成立"
    row = re.search(r"<tr[^>]*>(.*?)</tr>", html[start:], re.DOTALL)
    assert row is not None, "表格体里一行都没有，这几条用例的前提就不成立"
    cells = row.group(1).split("<td")
    assert len(cells) > SCORE_COLUMN, f"这一行只有 {len(cells) - 1} 格，取不到「目标军力」那一列"
    return cells[SCORE_COLUMN]


# -- 一、快照下来的值，事后被覆盖也不许跟着变 ------------------------------------


def test_the_score_is_snapshotted_when_the_intent_is_written(tmp_path: Path) -> None:
    """派遣时的军力被抄进 `attack_intents`，读数时刻一起。"""
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, SCORE_WHEN_DISPATCHED, OBSERVED)

    intent_id = _dispatch(repository, run_id, BOT_TARGET)

    with factory() as session:
        row = session.scalar(select(orm.AttackIntentRow).where(orm.AttackIntentRow.id == intent_id))
    assert row is not None
    assert row.target_military_score == SCORE_WHEN_DISPATCHED
    assert row.target_military_score_at_utc == OBSERVED


def test_a_later_rescan_does_not_move_the_number_on_the_log(tmp_path: Path) -> None:
    """**这一条是整个需求的判据。**

    先派一发，再让军力榜把 `bot_targets` 那一行整个刷掉（生产实测：31,756 →
    2,616）。日志上那一格必须仍然是派遣当时的 31,756——显示 2,616 就说明这一列
    连的是现值，而现值答的是「它现在多强」，不是「当时凭什么打它」。

    读数时刻同样不许跟着走：它是「这个分数有多旧」的唯一依据。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, SCORE_WHEN_DISPATCHED, OBSERVED)
    _dispatch(repository, run_id, BOT_TARGET)

    # 军力榜又采了一轮，同一个坐标的整行被覆盖。
    rescanned_at = DISPATCHED + timedelta(hours=6)
    _seed_score(repository, SCORE_AFTER_RESCAN, rescanned_at)

    cell = _score_cell(TestClient(create_persistent_app(factory)).get("/logs").text)

    assert "31,756" in cell, "日志显示的不是派遣当时的读数——这一列被连成了 `bot_targets` 现值"
    assert "2,616" not in cell, "重扫之后的现值出现在了日志上"
    # 读数时刻在角标的 `title` 里。OBSERVED（UTC 00:55）换算到现实时间 UTC+8 是 08:55。
    assert "派出时的读数：2026-08-18 08:55:00" in cell
    assert "2026-08-18 17:55" not in cell, "读数时刻跟着重扫走了"


# -- 二、没有读数是「—」，不是 0 -------------------------------------------------


def test_a_target_without_a_reading_shows_a_dash(tmp_path: Path) -> None:
    """海盗位在 `bot_targets` 里没有行，这一格显示「—」。

    ⚠️ **不能是 0。** 被打空的 bot 军力真的是 0，把「没读数」写成 0 等于让日志
    声称观测过一个从未发生的读数——而那正是这一列最容易出错、又最不显眼的方式。
    """
    repository, run_id, factory = _factory(tmp_path)

    _dispatch(repository, run_id, PIRATE_TARGET, kind=TARGET_KIND_PIRATE)

    cell = _score_cell(TestClient(create_persistent_app(factory)).get("/logs").text)

    assert "—" in cell, "没有军力读数时这一格该是「—」"
    assert "0" not in cell, "「没读数」被显示成了 0"
    assert TIP_CLASS not in cell, "没有快照却挂了个角标——点上去是一片空白，比没有更难判断"


def test_a_real_zero_reading_is_not_a_dash(tmp_path: Path) -> None:
    """反过来：军力真的是 0 的目标要显示 `0`，不能被当成「没读数」。

    这一条和上面那条是一对。只钉「None 显示成 —」的话，把两者一起归到「—」也
    照样绿，而那同样是在丢一个真实的观测。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, 0.0, OBSERVED)

    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(TestClient(create_persistent_app(factory)).get("/logs").text)

    assert "—" not in cell, "军力 0 被当成了「没读数」"
    assert re.search(r">\s*0\s*<", cell) is not None, "军力 0 没有原样显示出来"
    assert "派出时的读数：2026-08-18 08:55:00" in cell


# -- 三、历史行不许被回填 --------------------------------------------------------


def test_rows_written_before_this_column_existed_stay_empty(tmp_path: Path) -> None:
    """快照列为 NULL 的历史行照实显示「—」，**绝不拿 `bot_targets` 现值回填**。

    这一列 2026-08-18 才开始记；在那之前派出去的那些发次没有这个观测。补一个
    看起来合理的数进去，就再也分不出哪几行是真的读到过——那不是把日志补全，
    是往观测记录里掺编造。

    构造方式是照实模拟：先写意图（那时库里没有这个坐标的军力行，于是快照是
    NULL），事后军力榜才第一次采到它。页面上仍旧必须是「—」。
    """
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, BOT_TARGET)

    # 派完之后军力榜才第一次采到这个坐标。
    _seed_score(repository, SCORE_AFTER_RESCAN, DISPATCHED + timedelta(hours=6))

    html = TestClient(create_persistent_app(factory)).get("/logs").text
    cell = _score_cell(html)

    assert "—" in cell, "历史行被回填成了现值"
    assert "2,616" not in cell
    # 页面必须说清这一列从什么时候起才有，否则满屏「—」读起来就是
    # 「那时候的目标都没有军力读数」——和事实正好相反。
    assert "自 2026-08-18 起记录" in html


# -- 四、读数时刻藏在角标后面，不再平铺占一行 ------------------------------------


def test_the_snapshot_moment_hides_behind_a_hover_badge(tmp_path: Path) -> None:
    """有快照的行：军力值后面跟一个角标，时刻写在它的 `title` 里。

    ⚠️ **时刻不许再平铺成第二行。** 这一页刚为宽度打过两轮（#178 限宽、#183 加列），
    每格多一行就是整表多一行高度，而「这个分数什么时候读的」是排查时才要的一句。

    ⚠️ **时区要写进 tooltip 的文字里。** 表格里那几个时刻列靠表头标时区，而 tooltip
    没有表头——不写的话，UTC+0 的游戏时间和 UTC+8 的墙上时钟在这一格上分不出来，
    而这一页恰恰两种都在用。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, SCORE_WHEN_DISPATCHED, OBSERVED)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(TestClient(create_persistent_app(factory)).get("/logs").text)

    assert TIP_CLASS in cell, "有快照却没有角标，读数时刻就没地方看了"
    assert 'title="派出时的读数：2026-08-18 08:55:00（现实 UTC+8）"' in cell
    # 平铺那一行是这次要去掉的东西；它回来了就是又多占一行高度。
    assert "<div" not in cell, "读数时刻又被平铺成了一行"


def test_a_row_without_a_snapshot_has_no_badge_at_all(tmp_path: Path) -> None:
    """历史行**完全没有角标**，而不是挂一个空的 tooltip。

    2026-08-18 之前派出的那些发次本来就没有这个观测。摆一个点上去什么都没有的角标，
    比没有角标更难判断——用户会以为是 tooltip 坏了，而不是「当时没记这件事」。
    """
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(TestClient(create_persistent_app(factory)).get("/logs").text)

    assert "—" in cell
    assert TIP_CLASS not in cell, "没有快照的行挂上了角标——点开是一片空白"
    assert "派出时的读数" not in cell
