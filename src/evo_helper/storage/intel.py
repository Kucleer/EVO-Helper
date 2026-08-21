"""Intel search over the latest defender fleet snapshot per bot target.

Filtering happens on the server. The coordinate span is pushed into SQL, and so
is "latest report per target", so the browser never receives a target's whole
fleet history just to filter it.

The condition tree is then evaluated by the domain evaluator rather than
translated into SQL. The candidate set is already bounded by the span, and
reusing :meth:`ConditionGroup.matches` means the API and the tested domain
semantics cannot drift apart — an AND/OR tree compiled into SQL twice is two
implementations of the same rule.

一行 = 一个**目标星球**，而预设 / 派遣结果 / 战果全都挂在**派遣**上——同一个目标
可能被打过很多次，每次的预设与战果都不一样。这里的取值一律取**最近一次派遣**
（按意图创建时刻排的那一次），不是「打过就算」：

- 「打过就算」会让战果筛选自相矛盾——一个赢过也输过的目标同时属于「胜」和「负」，
  于是两个筛选谁都答不上「这个目标现在什么情况」。
- 操作台上要拿这一页去决定**下一发打谁**，而下一发看的是它现在的样子。

页面上必须把这条口径写出来（`intel.html` 的快速过滤那一栏），不然用户会以为
筛的是「历史上出现过」。

**这张表只装 bot 目标，海盗一行都不进**（用户口径 2026-08-14）。

理由是**海盗每 24 小时刷新一次**：今天侦察到「这颗星球上有 70 艘深空吞噬者」，
明天那支舰队连同那个海盗一起没了。而情报中心是**长期台账**——谁占着、舰队多少、
上次打是什么时候——台账里每一行都默认「它明天还是这样」。把一批第二天就作废的行
混进来，用户每次都得先分辨哪些还算数，而这正是台账要替他省掉的事。

海盗**不是不记**：每一发侦察 / 攻击仍然在攻击日志里。那一页一行是**一次派遣**，
是流水不是台账——流水本来就钉着时刻，24 小时刷新对它没有妨碍。

收在哪里见 `SqlAlchemyIntelRepository._rows_in_span`（行）与 `preset_names`
（「预设」下拉框的候选值）。**必须收在数据这一侧**：只在模板里跳过的话，
页面上的「共 N 条」、页码、以及快速过滤的候选值仍然按含海盗的那个集合算，
于是总数和看得见的行数对不上，而翻到后面几页全是空的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Select, select
from sqlalchemy.orm import Mapped, Session, sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.functions import func

from evo_helper.domain.battle_outcome import OUTCOME_PROTECTED
from evo_helper.domain.intel_query import (
    ConditionGroup,
    FleetCondition,
    GroupOperator,
    InvalidQueryError,
    Operator,
    QueryField,
)
from evo_helper.domain.models import Coordinate, CoordinateRange
from evo_helper.domain.records import (
    MISSION_KIND_SCOUT,
    TARGET_KIND_BOT,
    TARGET_KIND_PIRATE,
)
from evo_helper.domain.report_wait import MAX_REPORT_AGE
from evo_helper.storage import models as orm

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

SORT_COORDINATE = "coordinate"
SORT_TOTAL_DESC = "total_desc"
SORT_TOTAL_ASC = "total_asc"
SORT_SNAPSHOT_DESC = "snapshot_desc"
SORTS = (SORT_COORDINATE, SORT_TOTAL_DESC, SORT_TOTAL_ASC, SORT_SNAPSHOT_DESC)

#: 「结果」快速过滤的取值：最近一次派遣**有没有真的发出去**。
#:
#: 拦下与被拒是两回事，所以分成两档：`BLOCKED` 是本地闸门（读简报没通过、
#: 配额用完……）根本没点出去，`REJECTED` 是点了、游戏那边没接受。合成一档的话，
#: 「为什么没打」这个问题在页面上就没有答案了。
DISPATCH_SENT = "SENT"
DISPATCH_BLOCKED = "BLOCKED"
DISPATCH_REJECTED = "REJECTED"
DISPATCH_NEVER = "NEVER"
DISPATCH_STATES = (DISPATCH_SENT, DISPATCH_BLOCKED, DISPATCH_REJECTED, DISPATCH_NEVER)

#: 「战果」快速过滤的取值。
#:
#: `AWAITING`（待战报）只给**真的会有战报的那一发**：派出去、被接受、而且是攻击发。
#: 侦察发不产生战报（`domain.records.MISSION_KIND_SCOUT`），把它算成「待战报」
#: 会让页面上永远挂着一批等不到的行——PR #95 就是在认领那一侧踩的同一个坑。
RESULT_VICTORY = "VICTORY"
RESULT_FAIL = "FAIL"
RESULT_DRAW = "DRAW"
RESULT_AWAITING = "AWAITING"
#: 那一发的战报**永远不会来了**：派出至今超过 `MAX_REPORT_AGE`，一份战报都没接上。
#:
#: 它与 `AWAITING` 分家，是因为「还在等」和「等不到了」在页面上是两件事，而
#: 混成一档的代价只落在后者身上——一个消不掉的告警。
#:
#: **用户口径（2026-08-17）：「对账允许有对不上的情况，比如我手动操作的，只是读
#: 已经对上的。」** 助手点了「出发！」、用户随后手动把舰队撤了回来，那一发就
#: 永远不会产生战报；派遣记录是账、要留着，但它不该一直摆出「还在等」的样子。
#:
#: 6 小时这个界**不是这里新立的**：`repository.pending_reports_for_kind` /
#: `due_attack_dispatches` / `bot_dispatch_facts` 早就按同一个 `MAX_REPORT_AGE`
#: 把这类派遣判成「战报永远不会来」并整条剔掉了（判据现算，不写标记，理由见
#: `pending_reports_for_kind` 的 docstring）。情报中心这一格是唯一一处**没有**
#: 跟上那条界的地方，于是同一发派遣在调度那边早已结案、在页面上却还挂着黄色的
#: 「待战报」。这里补的就是这处不一致，不是新增一条时限。
RESULT_NO_REPORT = "NO_REPORT"
RESULT_NONE = "NONE"
#: 「到达时目标在保护期，舰队原路返航」——**打了但没打成**，见
#: `domain.battle_outcome.OUTCOME_PROTECTED`。
#:
#: ⚠️ 与 `NO_REPORT` 分家。两者都「没有战果」，但含义相反、能做的事也相反：
#: `NO_REPORT` 是**战报丢了**（可能是没翻到、可能是手动撤回），那一发到底打成
#: 什么样谁也不知道；这一档是**知道得清清楚楚**——没打起来，舰队原路飞回来了。
#: 混成一档就等于把一个已经查清的结论重新扔回「不明」那一堆。
#:
#: ⚠️ 它是 `battle_reports.outcome` 上真的存着的一个值，不像 `AWAITING` /
#: `NO_REPORT` / `NONE` 那三档是页面现算的（`_battle_result`）。所以它走的是
#: `if attempt.outcome is not None: return attempt.outcome` 那条原样透传的路。
RESULT_PROTECTED = OUTCOME_PROTECTED
BATTLE_RESULTS = (
    RESULT_VICTORY,
    RESULT_FAIL,
    RESULT_DRAW,
    RESULT_PROTECTED,
    RESULT_AWAITING,
    RESULT_NO_REPORT,
    RESULT_NONE,
)

#: 坐标的三个分量在 SQL 里的样子：ORM 属性（`orm.BotTargetRow.galaxy`）与子查询
#: 上的列（`latest.c.galaxy`）是两个类型，而打包比较对两者一视同仁。
_IntColumn = Mapped[int] | ColumnElement[int]


@dataclass(frozen=True)
class IntelSearchQuery:
    span: CoordinateRange | None = None
    conditions: ConditionGroup | None = None
    cursor: str | None = None
    limit: int = DEFAULT_LIMIT
    sort: str = SORT_COORDINATE
    #: 三个快速过滤，都按**最近一次派遣**判（见模块开头）。None = 不筛。
    preset: str | None = None
    dispatch_state: str | None = None
    battle_result: str | None = None

    def __post_init__(self) -> None:
        if self.limit < 1 or self.limit > MAX_LIMIT:
            raise InvalidQueryError(f"limit must be between 1 and {MAX_LIMIT}")
        if self.sort not in SORTS:
            raise InvalidQueryError(
                f"unknown sort {self.sort!r}; expected one of {', '.join(SORTS)}"
            )
        if self.dispatch_state is not None and self.dispatch_state not in DISPATCH_STATES:
            raise InvalidQueryError(
                f"unknown dispatch state {self.dispatch_state!r}; "
                f"expected one of {', '.join(DISPATCH_STATES)}"
            )
        if self.battle_result is not None and self.battle_result not in BATTLE_RESULTS:
            raise InvalidQueryError(
                f"unknown battle result {self.battle_result!r}; "
                f"expected one of {', '.join(BATTLE_RESULTS)}"
            )


@dataclass(frozen=True)
class IntelRow:
    coordinate: Coordinate
    player: str | None
    last_scan_at: datetime | None
    snapshot_at: datetime | None
    total: int | None
    counts: dict[str, int]
    matched_summary: str
    match_confidence: float | None
    review_status: str | None
    #: `bot` 还是 `pirate`（`domain.records.TARGET_KIND_*`）。列表按它上色。
    #:
    #: 海盗被收掉之后（见模块头）这里实际上只会是 `bot`，但字段留着：它是
    #: `_rows_in_span` 收人时用的那条判据本身，页面上的「类型」那一列也照它渲染。
    kind: str = TARGET_KIND_BOT
    #: 最近一次派遣的预设名 / 派遣结果 / 战果。见模块开头的口径说明。
    preset_name: str | None = None
    dispatch_state: str = DISPATCH_NEVER
    battle_result: str = RESULT_NONE
    #: 最近一份**侦察报告**的时间，以及它读到的四个判定舰种。
    #:
    #: ⚠️ **值可以是 `None`，而 `None` 不是 0**——那一格没读出来。整套
    #: ATTACK/SKIP/UNREADABLE 判定就建立在这个区分上
    #: （见 `storage.models.ScoutTriggerShipRow`），页面必须把两者显示成不同的东西。
    #:
    #: ⚠️ 这**不是舰队快照**，所以它不喂 `ConditionGroup`：那里只有四个判定舰种，
    #: 当成对方全部家当去算「舰队总数」会凭空缩水一个数量级。
    #:
    #: 侦察只对海盗做，而海盗已经不在这张表里（见模块头），所以这两格现在几乎总是
    #: 空的。取数与序列化那条路留着不动：一颗扫出来的 bot 身上要是挂了侦察报告，
    #: 它仍然要显示那一份，而「`None` 不是 0」这条规矩一旦从路上拆掉就再也补不回来。
    scout_at: datetime | None = None
    scout_ships: dict[str, int | None] = field(default_factory=dict)

    @property
    def has_fleet_data(self) -> bool:
        """有没有**舰队数字**，不是「有没有战报」。

        原先判的是 `snapshot_at is not None`，也就是「这个目标有战报」。bot 探路
        战报只读详情页、不写逐舰种行（打开逐舰种要进回放页，而那个入口按钮全仓
        没有标定坐标），于是 `counts` 为空、`total` 从「逐舰种求和」得到 0——
        页面上就成了「有舰队数据，总计 0」，而报告里明明写着守方单位 319。

        判 `total` 而不判 `snapshot_at`：读到了数才算有数。0 是合法的（对方真没船），
        所以比的是 `is not None` 而不是真值。
        """
        return self.total is not None

    @property
    def intel_at(self) -> datetime | None:
        """这一行最近一次「知道了点什么」的时刻：战报或侦察报告，取晚的那个。

        原先是为海盗写的：它们一份战报都没有，只按 `snapshot_at` 排会整批沉底。
        海盗离场之后（见模块头）绝大多数行的 `scout_at` 都是空的，这个取大值也就
        退化成 `snapshot_at`——但列名写的是「最新情报时间」，它就得说得出侦察那一份，
        否则一颗身上挂着侦察报告的 bot 会显示成「暂无情报」。
        """
        moments = [moment for moment in (self.snapshot_at, self.scout_at) if moment is not None]
        return max(moments) if moments else None


@dataclass(frozen=True)
class IntelSearchPage:
    rows: tuple[IntelRow, ...]
    next_cursor: str | None
    #: 当前条件下的**命中总数**，不是这一页的行数。翻页界面要说得出「共几条、
    #: 第几页」，光有 `next_cursor` 只能说出「还有没有下一页」。
    total: int = 0
    #: 这一页第一行在整个结果里的下标，页码由它和 limit 算出来。
    offset: int = 0


@dataclass(frozen=True)
class SavedFilter:
    filter_id: UUID
    name: str
    conditions: ConditionGroup
    span: CoordinateRange | None
    created_at_utc: datetime
    updated_at_utc: datetime


@dataclass(frozen=True)
class _Attempt:
    """最近一次派遣：意图 + （可能没有的）派遣 + （可能没有的）战报。"""

    target_kind: str
    preset_name: str
    dispatched_at: datetime | None
    accepted: bool | None
    mission_kind: str | None
    outcome: str | None
    #: 接上了一份战报没有。**与 `outcome` 分开**，理由同
    #: `web.service.AttackLogView.report_received`：战报收到了、可 OCR 没读出胜负
    #: 时 `outcome` 也是 None，而那一档绝不是「没有战报」——把它判成
    #: `RESULT_NO_REPORT` 就是拿一份真躺在库里的战报说它不存在。
    report_received: bool = False


@dataclass(frozen=True)
class _Report:
    reported_at: datetime
    defender_units: int | None
    match_confidence: float | None
    review_status: str | None
    counts: dict[str, int]


@dataclass(frozen=True)
class _Scout:
    reported_at: datetime
    ships: dict[str, int | None]


class SqlAlchemyIntelRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    # -- search ------------------------------------------------------------

    def search(self, query: IntelSearchQuery) -> IntelSearchPage:
        """一次检索 = 五条**成批**查询，不是「每个目标再查一遍」。

        原先每个目标都单独查一次最新战报再查一次逐舰种，全宇宙 4000 多个 bot
        就是 8000 多次往返。取数改成按 span 一次取全（`row_number()` 挑出每个目标
        最新那一份），行数仍由 span 兜住。

        筛选（条件树 + 三个快速过滤）在内存里做，而这与攻击日志那条「必须下推
        SQL」的教训**不矛盾**：那边先按 limit 砍掉历史再筛，查旧账必得空页；
        这边候选集是 span 内的全部目标、分页在筛选**之后**才切，一行都没被提前砍掉。
        """
        with self._session_factory() as session:
            rows = self._rows_in_span(session, query.span)
        rows = [row for row in rows if _passes_quick_filters(row, query)]
        if query.conditions is not None:
            matched = []
            for row in rows:
                counts = row.counts or None
                if query.conditions.matches(counts):
                    summary = ", ".join(query.conditions.matched_labels(counts))
                    matched.append(replace(row, matched_summary=summary))
            rows = matched
        rows = _sorted(rows, query.sort)
        return _paginate(rows, cursor=query.cursor, limit=query.limit)

    def _rows_in_span(self, session: Session, span: CoordinateRange | None) -> list[IntelRow]:
        """span 内的每一个**目标星球**一行——**海盗在最后一步被收掉**。

        海盗不在 `bot_targets` 里：那张表由坐标扫描写，而海盗是在星系视图上认出来
        的。所以候选集仍然是三者的并集：bot 目标、有过海盗派遣的坐标、有过侦察报告
        的坐标——并集照旧取全，收在**类型**这一步（海盗 24 小时刷新、留不成台账，
        理由见模块头）。

        为什么不干脆把海盗从并集里摘掉：

        - 谁是海盗只有 `_kind_of` 说了算。摘一次等于把同一条判据写第二份，将来两份
          各自漂移；而「bot 目标身上却挂着一份侦察报告」那种行，也只有 `_kind_of`
          分得清（它判成 bot——`bot_targets` 是正面证据）。
        - 并集里多出来的那几十个坐标**不多花一次查询**：`attempts` 与 `scouts` 本来
          就要取（bot 行的预设 / 结果 / 战果全在 `attempts` 里），并集只是把已经在手
          的字典拼一下。

        收在这里而不是更靠外的地方：`search()` 的计数与分页都发生在这之后，
        所以「共 N 条」、页码和看得见的行数说的是同一批行。
        """
        targets = self._bot_targets(session, span)
        attempts = self._latest_attempts(session, span)
        reports = self._latest_reports(session, span)
        scouts = self._latest_scouts(session, span)
        coordinates = (
            set(targets)
            | set(scouts)
            | {
                coordinate
                for coordinate, attempt in attempts.items()
                if attempt.target_kind == TARGET_KIND_PIRATE
            }
        )
        # 整批行共用**同一个**此刻：逐行各读一次的话，「这一发算不算等不到了」
        # 会在同一张表里按两个略微不同的时刻判，而那正好是排序与分页最怕的抖动。
        now = datetime.now(UTC)
        rows = [
            _build_row(
                coordinate,
                targets.get(coordinate),
                attempts.get(coordinate),
                reports.get(coordinate),
                scouts.get(coordinate),
                now=now,
            )
            for coordinate in coordinates
        ]
        # 海盗不进这张表（理由见方法头与模块头）。整个筛选只有这一条判据，
        # 别在别处再补一次——两处过滤就意味着有一天两处会不一致。
        return [row for row in rows if row.kind != TARGET_KIND_PIRATE]

    def _bot_targets(
        self, session: Session, span: CoordinateRange | None
    ) -> dict[Coordinate, orm.BotTargetRow]:
        statement = select(orm.BotTargetRow).where(orm.BotTargetRow.is_bot)
        statement = _within(
            statement,
            span,
            orm.BotTargetRow.galaxy,
            orm.BotTargetRow.system,
            orm.BotTargetRow.position,
        )
        return {
            Coordinate(row.galaxy, row.system, row.position): row
            for row in session.scalars(statement)
        }

    def _latest_attempts(
        self, session: Session, span: CoordinateRange | None
    ) -> dict[Coordinate, _Attempt]:
        """每个目标**最近一次**派遣，按意图创建时刻排。

        战报按 `dispatch_id` 接上来，不按坐标重配：那是仓储层做过时间与坐标核对
        之后写下的匹配结果，在这里重配一次就是把同一条判据写第二份。
        """
        intent = orm.AttackIntentRow
        dispatch = orm.AttackDispatchRow
        report = orm.BattleReportRow
        ranked = _within(
            select(
                intent.target_galaxy.label("galaxy"),
                intent.target_system.label("system"),
                intent.target_position.label("position"),
                intent.target_kind.label("target_kind"),
                intent.preset_name.label("preset_name"),
                dispatch.dispatched_at_utc.label("dispatched_at"),
                dispatch.accepted.label("accepted"),
                dispatch.mission_kind.label("mission_kind"),
                report.outcome.label("outcome"),
                # 战报**行**在不在，和它读没读出胜负是两件事，见 `_Attempt.report_received`。
                # 这一列不额外多一次连接：`report` 本来就已经外连接上来了。
                report.id.label("report_id"),
                func.row_number()
                .over(
                    partition_by=(
                        intent.target_galaxy,
                        intent.target_system,
                        intent.target_position,
                    ),
                    order_by=(intent.created_at_utc.desc(), intent.id.desc()),
                )
                .label("row_no"),
            )
            .outerjoin(dispatch, dispatch.intent_id == intent.id)
            .outerjoin(report, report.dispatch_id == dispatch.id),
            span,
            intent.target_galaxy,
            intent.target_system,
            intent.target_position,
        ).subquery()
        rows = session.execute(select(ranked).where(ranked.c.row_no == 1)).all()
        return {
            Coordinate(row.galaxy, row.system, row.position): _Attempt(
                target_kind=row.target_kind,
                preset_name=row.preset_name,
                dispatched_at=row.dispatched_at,
                accepted=row.accepted,
                mission_kind=row.mission_kind,
                outcome=row.outcome,
                report_received=row.report_id is not None,
            )
            for row in rows
        }

    def _latest_reports(
        self, session: Session, span: CoordinateRange | None
    ) -> dict[Coordinate, _Report]:
        report = orm.BattleReportRow
        ranked = _within(
            select(
                report.id.label("report_id"),
                report.defender_target_galaxy.label("galaxy"),
                report.defender_target_system.label("system"),
                report.defender_target_position.label("position"),
                report.reported_at_utc.label("reported_at"),
                report.defender_units.label("defender_units"),
                report.match_confidence.label("match_confidence"),
                report.manual_review_status.label("review_status"),
                func.row_number()
                .over(
                    partition_by=(
                        report.defender_target_galaxy,
                        report.defender_target_system,
                        report.defender_target_position,
                    ),
                    order_by=(report.reported_at_utc.desc(), report.id.desc()),
                )
                .label("row_no"),
            ),
            span,
            report.defender_target_galaxy,
            report.defender_target_system,
            report.defender_target_position,
        ).subquery()
        latest = select(ranked).where(ranked.c.row_no == 1).subquery()
        counts = _defender_counts(session, latest)
        return {
            Coordinate(row.galaxy, row.system, row.position): _Report(
                reported_at=row.reported_at,
                defender_units=row.defender_units,
                match_confidence=row.match_confidence,
                review_status=row.review_status,
                counts=counts.get(Coordinate(row.galaxy, row.system, row.position), {}),
            )
            for row in session.execute(select(latest)).all()
        }

    def _latest_scouts(
        self, session: Session, span: CoordinateRange | None
    ) -> dict[Coordinate, _Scout]:
        """每个目标最近一份侦察报告，连同它读到的四个判定舰种。

        ⚠️ `count` 原样读回，`NULL` 保持 `None`。**不许 `or 0`**：0 是「这里没有
        这种船」，`None` 是「这一格没读出来」，把后者记成前者就是把一支实打实的
        舰队记成空的（见 `storage.models.ScoutTriggerShipRow`）。
        """
        scout = orm.ScoutReportRow
        ranked = _within(
            select(
                scout.id.label("report_id"),
                scout.target_galaxy.label("galaxy"),
                scout.target_system.label("system"),
                scout.target_position.label("position"),
                scout.reported_at_utc.label("reported_at"),
                func.row_number()
                .over(
                    partition_by=(scout.target_galaxy, scout.target_system, scout.target_position),
                    order_by=(scout.reported_at_utc.desc(), scout.id.desc()),
                )
                .label("row_no"),
            ),
            span,
            scout.target_galaxy,
            scout.target_system,
            scout.target_position,
        ).subquery()
        latest = select(ranked).where(ranked.c.row_no == 1).subquery()
        ships: dict[Coordinate, dict[str, int | None]] = {}
        trigger = orm.ScoutTriggerShipRow
        for galaxy, system, position, ship_type, count in session.execute(
            select(
                latest.c.galaxy,
                latest.c.system,
                latest.c.position,
                trigger.ship_type,
                trigger.count,
            )
            .join(trigger, trigger.report_id == latest.c.report_id)
            .order_by(trigger.ordinal)
        ).all():
            ships.setdefault(Coordinate(galaxy, system, position), {})[ship_type] = count
        return {
            Coordinate(row.galaxy, row.system, row.position): _Scout(
                reported_at=row.reported_at,
                ships=ships.get(Coordinate(row.galaxy, row.system, row.position), {}),
            )
            for row in session.execute(select(latest)).all()
        }

    def preset_names(self) -> list[str]:
        """**bot 派遣**里出现过的预设名，供「预设」快速过滤的下拉框用。

        取自 `attack_intents` 而不是写死一张表：预设是用户在游戏里配的
        （探路 / AAA / BBB / CCC / 侦察……），写死就意味着新加一个预设之后，
        这一页会安静地筛不到它。

        只取 `target_kind = bot` 的那些：这张表里已经没有海盗了（见模块头），
        海盗专用的预设（实机上「侦察」那 522 发全是海盗）留在下拉框里，就是留一个
        **选下去必然是空页**的选项——那比没有这个选项更糟：用户会以为侦察记录被弄丢
        了，而它们一直在攻击日志里。
        """
        with self._session_factory() as session:
            return sorted(
                name
                for name in session.scalars(
                    select(orm.AttackIntentRow.preset_name)
                    .where(orm.AttackIntentRow.target_kind == TARGET_KIND_BOT)
                    .distinct()
                )
                if name
            )

    # -- saved filters -----------------------------------------------------

    def save_filter(
        self,
        *,
        name: str,
        conditions: ConditionGroup,
        span: CoordinateRange | None = None,
        filter_id: UUID | None = None,
    ) -> SavedFilter:
        cleaned = name.strip()
        if not cleaned:
            raise InvalidQueryError("a saved filter needs a name")
        now = datetime.now(UTC)
        with self._session_factory() as session:
            row = session.get(orm.IntelFilterRow, filter_id) if filter_id else None
            if row is None:
                row = orm.IntelFilterRow(id=filter_id or uuid4(), created_at_utc=now)
                session.add(row)
            row.name = cleaned
            row.condition_tree = json.dumps(encode_group(conditions), ensure_ascii=False)
            row.span_start = str(span.start) if span else None
            row.span_end = str(span.end) if span else None
            row.updated_at_utc = now
            session.commit()
            return _to_saved_filter(row)

    def get_filter(self, filter_id: UUID) -> SavedFilter | None:
        with self._session_factory() as session:
            row = session.get(orm.IntelFilterRow, filter_id)
            return _to_saved_filter(row) if row else None

    def list_filters(self) -> list[SavedFilter]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.IntelFilterRow).order_by(orm.IntelFilterRow.name)
            ).all()
            return [_to_saved_filter(row) for row in rows]

    def delete_filter(self, filter_id: UUID) -> None:
        with self._session_factory() as session:
            row = session.get(orm.IntelFilterRow, filter_id)
            if row is not None:
                session.delete(row)
                session.commit()

    def known_ship_names(self) -> set[str]:
        """Every defender ship type the project has actually recorded."""
        with self._session_factory() as session:
            return set(
                session.scalars(
                    select(orm.FleetSnapshotRow.ship_type)
                    .where(orm.FleetSnapshotRow.side == "defender")
                    .distinct()
                )
            )


# -- condition tree serialisation ------------------------------------------


def encode_group(group: ConditionGroup) -> dict[str, object]:
    return {
        "type": "group",
        "operator": group.operator.value,
        "children": [
            encode_group(child) if isinstance(child, ConditionGroup) else _encode_condition(child)
            for child in group.children
        ],
    }


def decode_group(payload: dict[str, object]) -> ConditionGroup:
    if payload.get("type") != "group":
        raise InvalidQueryError("expected a condition group at the top of the tree")
    raw_children = payload.get("children")
    if not isinstance(raw_children, list):
        raise InvalidQueryError("a condition group needs a list of children")
    children: list[FleetCondition | ConditionGroup] = []
    for child in raw_children:
        if not isinstance(child, dict):
            raise InvalidQueryError("each condition must be an object")
        children.append(
            decode_group(child) if child.get("type") == "group" else _decode_condition(child)
        )
    return ConditionGroup(
        operator=_decode_group_operator(payload.get("operator")), children=tuple(children)
    )


def _encode_condition(condition: FleetCondition) -> dict[str, object]:
    return {
        "type": "condition",
        "field": condition.field.ship_type or "__total__",
        "operator": condition.operator.value,
        "value": condition.value,
    }


def _decode_condition(payload: dict[str, object]) -> FleetCondition:
    field_name = payload.get("field")
    if not isinstance(field_name, str) or not field_name:
        raise InvalidQueryError("a condition needs a field")
    field = QueryField.total() if field_name == "__total__" else QueryField.ship(field_name)
    raw_value = payload.get("value")
    if not isinstance(raw_value, int) or isinstance(raw_value, bool):
        raise InvalidQueryError(f"{field.label} needs a whole-number value")
    return FleetCondition(
        field=field, operator=_decode_operator(payload.get("operator")), value=raw_value
    )


def _decode_operator(raw: object) -> Operator:
    if not isinstance(raw, str):
        raise InvalidQueryError(f"unknown operator {raw!r}")
    try:
        return Operator(raw)
    except ValueError as exc:
        raise InvalidQueryError(f"unknown operator {raw!r}") from exc


def _decode_group_operator(raw: object) -> GroupOperator:
    if not isinstance(raw, str):
        raise InvalidQueryError(f"unknown group operator {raw!r}; expected AND or OR")
    try:
        return GroupOperator(raw)
    except ValueError as exc:
        raise InvalidQueryError(f"unknown group operator {raw!r}; expected AND or OR") from exc


# -- helpers ----------------------------------------------------------------


def _pack(coordinate: Coordinate) -> int:
    return (coordinate.galaxy * 1000 + coordinate.system) * 1000 + coordinate.position


def _packed_column(
    galaxy: _IntColumn, system: _IntColumn, position: _IntColumn
) -> ColumnElement[int]:
    return galaxy * 1_000_000 + system * 1000 + position


def _within(
    statement: Select[Any],
    span: CoordinateRange | None,
    galaxy: _IntColumn,
    system: _IntColumn,
    position: _IntColumn,
) -> Select[Any]:
    """把坐标区间下推成**一次**打包整数的范围比较。

    逐分量比较（galaxy>=… AND system>=… AND position>=…）会把 1:150:4 排除在
    1:100:1 – 1:200:999 之外，因为 4 < 1 不成立那一路走不通。打包成一个整数之后，
    区间就是它本来的样子。
    """
    if span is None:
        return statement
    return statement.where(
        _packed_column(galaxy, system, position).between(_pack(span.start), _pack(span.end))
    )


def _passes_quick_filters(row: IntelRow, query: IntelSearchQuery) -> bool:
    """三个快速过滤，全部按**最近一次派遣**判（见模块开头）。None = 不筛。"""
    if query.preset is not None and row.preset_name != query.preset:
        return False
    if query.dispatch_state is not None and row.dispatch_state != query.dispatch_state:
        return False
    return not (query.battle_result is not None and row.battle_result != query.battle_result)


def _build_row(
    coordinate: Coordinate,
    target: orm.BotTargetRow | None,
    attempt: _Attempt | None,
    report: _Report | None,
    scout: _Scout | None,
    *,
    now: datetime,
) -> IntelRow:
    return IntelRow(
        coordinate=coordinate,
        player=target.latest_owner_name if target else None,
        last_scan_at=target.last_scanned_at_utc if target else None,
        snapshot_at=report.reported_at if report else None,
        # 逐舰种有行就按行求和；一行都没有时退回战报详情页上的守方「单位」总数。
        # 这两个是**两个独立来源**，不是同一个数的两种写法：大舰队的逐行数量是
        # 四舍五入显示的，相加凑不出精确总数（见 `records.BattleReport` 的注释）。
        # 所以优先用逐行和——它带着构成信息；没有逐行时用总数，总比显示 0 强。
        total=(
            None
            if report is None
            else (sum(report.counts.values()) if report.counts else report.defender_units)
        ),
        counts=dict(report.counts) if report else {},
        matched_summary="",
        match_confidence=report.match_confidence if report else None,
        review_status=report.review_status if report else None,
        kind=_kind_of(target, attempt, scout),
        preset_name=attempt.preset_name if attempt else None,
        dispatch_state=_dispatch_state(attempt),
        battle_result=_battle_result(attempt, now=now),
        scout_at=scout.reported_at if scout else None,
        scout_ships=dict(scout.ships) if scout else {},
    )


def _kind_of(
    target: orm.BotTargetRow | None, attempt: _Attempt | None, scout: _Scout | None
) -> str:
    """派遣写下的 `target_kind` 最有分量：那是真打出去的那一发自己记的。

    没派过就看 `bot_targets` 里有没有这一行——那是坐标扫描认出来的 bot 星球，
    是**正面证据**；两样都没有才退回「有侦察报告 = 海盗」这条**推断**
    （侦察只对海盗做）。

    ⚠️ 这个顺序不能反。类型现在决定**这一行进不进这张表**（海盗不进，见模块头），
    所以把一颗扫出来的 bot 因为身上多了一份侦察报告判成海盗，等于从台账里删掉
    一行——从前判错只是 chip 上错了个色，现在是丢数据。
    """
    if attempt is not None:
        return attempt.target_kind
    if target is not None:
        return TARGET_KIND_BOT
    if scout is not None:
        return TARGET_KIND_PIRATE
    return TARGET_KIND_BOT


def _dispatch_state(attempt: _Attempt | None) -> str:
    if attempt is None:
        return DISPATCH_NEVER
    if attempt.dispatched_at is None:
        return DISPATCH_BLOCKED
    return DISPATCH_SENT if attempt.accepted else DISPATCH_REJECTED


def _battle_result(attempt: _Attempt | None, *, now: datetime) -> str:
    """战果只对「真的飞出去的攻击发」有意义。

    `outcome` 原样返回，不拿「不是 VICTORY 就算负」兜底：库里存的是画面原文，
    将来多一档会被静默显示成败仗（`logs.html` 上同一条取舍）。

    没有战报时还要再分一次「还在等」与「等不到了」，判据是同一个
    `MAX_REPORT_AGE`——理由与它是从哪儿借来的，都写在 `RESULT_NO_REPORT` 上。

    **判据现算，不写标记**，与 `repository.pending_reports_for_kind` 同一个理由：
    写标记要有人在每一发到期的那一刻去写，而那个人不存在；先落地标记再依赖它，
    中间这段时间页面会一行都排不掉。所以这里要一个 `now`，而不是去读某个列。

    `report_received` 那一档**不许并进来**：战报收到了、只是没读出胜负时
    `outcome` 同样是 None，判成「无战报」就是拿一份真躺在库里的战报说它不存在。
    """
    if attempt is None or attempt.dispatched_at is None or not attempt.accepted:
        return RESULT_NONE
    if attempt.mission_kind == MISSION_KIND_SCOUT:
        return RESULT_NONE
    if attempt.outcome is not None:
        return attempt.outcome
    if not attempt.report_received and attempt.dispatched_at <= now - MAX_REPORT_AGE:
        return RESULT_NO_REPORT
    return RESULT_AWAITING


def _defender_counts(session: Session, latest: Any) -> dict[Coordinate, dict[str, int]]:
    """Counts from the participating fleet, which is the pre-battle holding.

    Per-round rows carry a ``round_no`` and describe what survived each round;
    including them would multiply-count every ship type.

    ``latest`` 是「每个目标最新那一份战报」的子查询，直接 join 上去而不是先把
    战报 id 取回来再拼一条 `IN (...)`：span 里有几千个目标时那串参数本身就是负担。
    """
    snapshot = orm.FleetSnapshotRow
    counts: dict[Coordinate, dict[str, int]] = {}
    for galaxy, system, position, ship_type, count in session.execute(
        select(
            latest.c.galaxy,
            latest.c.system,
            latest.c.position,
            snapshot.ship_type,
            snapshot.count,
        )
        .join(snapshot, snapshot.report_id == latest.c.report_id)
        .where(snapshot.side == "defender", snapshot.round_no.is_(None))
    ).all():
        counts.setdefault(Coordinate(galaxy, system, position), {})[ship_type] = count
    return counts


def _to_saved_filter(row: orm.IntelFilterRow) -> SavedFilter:
    span = None
    if row.span_start and row.span_end:
        span = CoordinateRange(start=_parse_stored(row.span_start), end=_parse_stored(row.span_end))
    return SavedFilter(
        filter_id=row.id,
        name=row.name,
        conditions=decode_group(json.loads(row.condition_tree)),
        span=span,
        created_at_utc=row.created_at_utc,
        updated_at_utc=row.updated_at_utc,
    )


def _parse_stored(text: str) -> Coordinate:
    galaxy, system, position = (int(part) for part in text.split(":"))
    return Coordinate(galaxy, system, position)


def _sorted(rows: list[IntelRow], sort: str) -> list[IntelRow]:
    if sort == SORT_TOTAL_DESC:
        return sorted(rows, key=lambda r: (-(r.total or -1), _pack(r.coordinate)))
    if sort == SORT_TOTAL_ASC:
        return sorted(
            rows, key=lambda r: ((r.total if r.total is not None else 1 << 30), _pack(r.coordinate))
        )
    if sort == SORT_SNAPSHOT_DESC:
        # `intel_at` 而不是 `snapshot_at`：一行的「情报」也可能只是一份侦察报告。
        # 海盗离场后这两者对绝大多数行是同一个值（见 `IntelRow.intel_at`），
        # 但排序键与列名要说的是同一件事。
        return sorted(
            rows,
            key=lambda r: (
                -(r.intel_at.timestamp() if r.intel_at else float("-inf")),
                _pack(r.coordinate),
            ),
        )
    return sorted(rows, key=lambda r: _pack(r.coordinate))


def _paginate(rows: list[IntelRow], *, cursor: str | None, limit: int) -> IntelSearchPage:
    """Cursor is the index into the ordered result, encoded as a string.

    The candidate set is already bounded by the coordinate span, so an offset
    cursor is honest here; a keyset cursor would buy nothing and would have to
    encode the active sort key.
    """
    start = _decode_cursor(cursor)
    page = rows[start : start + limit]
    next_index = start + limit
    return IntelSearchPage(
        rows=tuple(page),
        next_cursor=str(next_index) if next_index < len(rows) else None,
        total=len(rows),
        offset=start,
    )


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except ValueError as exc:
        raise InvalidQueryError(f"invalid cursor {cursor!r}") from exc
    if value < 0:
        raise InvalidQueryError("cursor cannot be negative")
    return value
