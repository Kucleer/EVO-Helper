"""攻击日志的「目标军力」那一格：插出来的数要当场标出来——用**黄色的 ⓘ**，不用中文。

## 标记从「(估算)」改成黄色角标

用户口径（2026-08-19）：「这里目标军力如果是估算的，icon 换成黄色，不要出现中文
影响页面美观」。此前那一格平铺三个字 `9,325 (估算)ⓘ`，加上分数超出列宽、折成两行，
**整行**因此比别的行高——一屏记录参差不齐。这一页为宽度收拾过两轮（#178 限宽、
#183 加列），版面预算里没有这三个字的位置。

⚠️ **删字不等于删信息。** 判据是「不看颜色的人也得知道这是估算值」，靠两条腿站着，
少一条这个需求就没做完：

1. 角标的 `title` 里写全「这个数不是读到的，是插出来的」——和读数时刻同一个
   原生 tooltip，不新起组件；
2. 表格上方那段说明按「ⓘ 是黄色的那几行」描述——颜色本身不自我说明，得有一句话
   把颜色和含义接起来，否则黄色就是个没人认识的装饰。

第四、五节各钉一条，第二节钉「中文不许回来」。

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
清成 `False`（2026-08-18 跑过两次）。事后现取，今天标着估算的记录明天会自己变成
「实读」。第一节钉的就是这件事。

## 三档，不是两档

`True` 染黄、`False` 不染、`None` 也不染——但两种「不染」含义相反：`False` 是
「这个数是实读的」，`None` 是「2026-08-18 之前派的，当时没记这件事」。第三节钉住
`None` 既不被回填成 `False`、页面也不替它声称实读。
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
PRESET = FleetPresetRef(name="BBB", signature="预设:BBB")

#: 生产实测的那一条：`4:336:11 = 72,252（估算）`，2026-08-18 的清理把它的
#: `estimated` 从 `True` 清成了 `False`——正是「事后现取会改口」的实证。
ESTIMATED_SCORE = 72252.0

#: 估算那一档的角标类名。**按类名认，不按颜色认**：颜色在样式表里，第四节单钉。
ESTIMATED_TIP_CLASS = 'class="score-tip estimated"'

#: 读数时刻那个角标的类名（#183 加的）。按类名认，不按字符认。
TIP_CLASS = 'class="score-tip'


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

    ⚠️ **只搜这一格，绝不整页搜。** 表格上方那句说明里也谈论着估算（它必须在那儿：
    黄色不解释一遍就是个没人认识的装饰）。整页搜「这一行是不是估算」的话，无论哪一行
    是什么样，那一条都永远绿。
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


def _console_css() -> str:
    """样式表，**注释先剥掉**。

    ⚠️ 这个仓库里注释比代码长，而注释里成段引用着规则本身。不剥的话，「这条规则
    还在不在」的断言会被一句谈论它的注释喂饱——真把规则删了也照样绿。
    （同 `test_attack_log_width.py` 里那个同名助手。）
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


# -- 一、事后重扫改了 estimated，日志那一格不跟着变 ------------------------------


def test_a_later_rescan_cannot_unmark_an_estimated_reading(tmp_path: Path) -> None:
    """**这一条是整个需求的判据。**

    先在标着估算的时候派一发，再让军力榜把 `bot_targets` 那一行整个刷成实读。
    日志上那一格必须**仍然**是估算那一档——不是的话就说明这一列连的是现值，而现值
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

    assert ESTIMATED_TIP_CLASS in cell, "重扫把日志上的估算标记抹掉了——这一格被连成了现值"
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
    assert ESTIMATED_TIP_CLASS in cell, "清空 `bot_targets` 的读数把日志上的估算标记也带走了"


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


# -- 二、True 染黄角标，而且中文不许回来 -----------------------------------------


def test_an_estimated_reading_is_marked_by_a_highlighted_badge(tmp_path: Path) -> None:
    """`True`：分数后面只跟一个角标，角标挂上 `estimated` 这一档。"""
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(_logs(factory))

    assert ESTIMATED_TIP_CLASS in cell, "插出来的数没有任何标记，页面在把它当实读展示"
    assert "72,252" in cell
    assert cell.index("72,252") < cell.index(ESTIMATED_TIP_CLASS), "标记跑到了分数前面"


def test_the_cell_carries_no_chinese_label_any_more(tmp_path: Path) -> None:
    """⚠️ **这一条就是用户 2026-08-19 那句口径本身。**

    「不要出现中文影响页面美观」——`(估算)` 那三个字加上分数超出列宽、把那一格折成
    两行，整行因此比别的行高。把中文标记改回去（哪怕只是「(估)」「估算」）这一条就红。

    连带钉住整格仍旧**一行**：`<div` 不许出现。这一格多一行就是整表多一行高度，
    而这一页为宽度收拾过两轮。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(_logs(factory))
    # `title` 里是要写中文的（不看颜色的人靠它），所以只看渲染出来的可见文本。
    visible = re.sub(r"<[^>]*>", "", cell)

    assert "估算" not in visible, "「估算」两个字回到了格子里——列宽放不下，那一行会被折高"
    assert "<div" not in cell, "这一格长出了第二行"


def test_a_real_reading_is_not_highlighted(tmp_path: Path) -> None:
    """`False`：角标照出（它还带着读数时刻），但**不进估算那一档**。

    这一条和上面那条是一对。只钉「True 时挂上」的话，把 `estimated` 无条件贴在每一行
    上也照样绿——而那种页面等于没有标记，读者分不出哪几行是插出来的。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=False)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(_logs(factory))

    assert "72,252" in cell
    assert TIP_CLASS in cell, "实读的行连读数时刻的角标都没了"
    assert ESTIMATED_TIP_CLASS not in cell, "实读的分数被标成了估算"
    assert "估算" not in cell, "实读的行在 `title` 里被说成了估算"


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
    # 页面上两边都不说：既不标估算，也没有一个反过来声称实读的标记。
    assert ESTIMATED_TIP_CLASS not in cell, "历史行被标成了估算"
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


# -- 四、不看颜色也得知道：`title` 说清，样式表用既有的黄 --------------------------


def test_the_badge_title_spells_out_that_the_number_was_not_read(tmp_path: Path) -> None:
    """⚠️ **这是「删了中文之后信息没丢」的第一条腿。**

    格子里只剩一个 ⓘ，那么「这个数不是读到的」这句话必须落在它的 `title` 上。
    `title` 一丢，页面就退回成「靠颜色猜」——灰度屏、色盲用户、以及任何没被告知过
    黄色代表什么的人，都会把插值当实读读。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(_logs(factory))
    title = re.search(r'class="score-tip estimated" title="(.*?)"', cell, re.DOTALL)

    assert title is not None, "估算那个角标没有 `title`——删掉中文之后什么都不剩了"
    text = title.group(1)
    assert "不是实读" in text, "`title` 没说清这个数不是读到的"
    assert "插" in text, "`title` 没说清这个数是插出来的"


def test_the_badge_still_carries_the_reading_moment(tmp_path: Path) -> None:
    """读数时刻仍在同一个 `title` 里——两句话合并到一个角标上，不是二选一。

    #183 加的那句话不许被这次改动挤掉：2026-08-17 一整天的排障反复卡在
    「这个分数是什么时候读的」。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(_logs(factory))

    assert "派出时的读数：2026-08-18 08:55:00（现实 UTC+8）" in cell
    # 一个角标，不是两个：合并之后不许还留着旧的那一个。
    assert cell.count("score-tip") == 1, "这一格挂了不止一个角标"


def test_the_highlight_uses_the_existing_warning_colour(tmp_path: Path) -> None:
    """黄色取页面既有的 `--warn`，**不新造色值**。

    同一套控制台上「要留意」只该有一种黄（`.chip.warn`、状态里那些「?」都用它）；
    在这里写死一个 `#ffcc00` 之类的，读者会以为它和别处的黄是两回事。

    顺带钉住「只改颜色」：这条规则里出现任何会改变尺寸的属性（字号、粗细、
    padding、border），就是把「那一行被折高」的问题原样改回去。
    """
    rule = re.search(r"\.score-tip\.estimated\s*\{([^}]*)\}", _console_css())

    assert rule is not None, "样式表里没有 `.score-tip.estimated`——黄色从来没生效过"
    body = rule.group(1)
    assert "var(--warn)" in body, "估算那一档的黄不是页面既有的语义色"
    for forbidden in ("font-size", "font-weight", "padding", "border", "display", "line-height"):
        assert forbidden not in body, f"这条规则改了 `{forbidden}`，会把那一行重新撑高"


def test_a_history_row_still_has_no_badge(tmp_path: Path) -> None:
    """历史行仍旧一个角标都没有——既没有快照时刻、也不是估算，就不该凭空长出一个
    点上去什么都没有的 tooltip。"""
    repository, run_id, factory = _factory(tmp_path)
    _dispatch(repository, run_id, BOT_TARGET)

    cell = _score_cell(_logs(factory))

    assert "score-tip" not in cell, "没有快照的行长出了角标——点开是一片空白"
    assert "派出时的读数" not in cell


# -- 五、页面得把「黄色」和「估算」接起来 ----------------------------------------


def test_the_page_explains_what_the_yellow_badge_means(tmp_path: Path) -> None:
    """⚠️ **这是「删了中文之后信息没丢」的第二条腿。**

    颜色不自我说明：页面上得有一句话讲「军力后面的 ⓘ 是黄色的，说的是这个数不是
    读到的」。说明必须落在页面上，不能只写在代码注释里——看这一页的人不看代码。

    ⚠️ **措辞必须跟着格子里的标记走。** 留着旧的「标 (估算) 的那几行」就是让说明去
    指一个页面上根本不存在的标记，读者照着找永远找不到，比不解释更糟。
    """
    repository, run_id, factory = _factory(tmp_path)
    _seed_score(repository, ESTIMATED_SCORE, OBSERVED, estimated=True)
    _dispatch(repository, run_id, BOT_TARGET)

    html = _logs(factory)
    intro = html[: html.find("<tbody")]

    assert "黄色" in intro, "说明里没提颜色——那个黄角标没有任何人认识"
    assert "ⓘ" in intro, "说明里没提是哪个标记变黄"
    assert "不是实读" in intro, "说明没讲清这个数不是读到的"
    assert "(估算)" not in intro, "说明还在指「(估算)」，而页面上已经没有这三个字了"
