"""军力榜的读写。

## 两个数据源，用途不同

| | `military_ranking_*`（快照） | `bot_targets`（实时） |
|---|---|---|
| 谁写 | 只有 `POST /api/military-rankings/snapshots` | `ranking_scan` 每读一屏就写 |
| 页面读哪个 | ~~曾经是它~~ | **现在是它** |

⚠️ **`/rankings` 页面曾经读快照表，而那张表没有活着的写入方。** 2026-08-16 查明：
`ranking_scan.persist()` 写的是 `bot_targets`（`save_ranking_targets`），全仓没有任何
代码调用过那个 POST 接口，所以库里唯一那份快照是迁移 `fa1c3d4e5f67` 从
`bot_targets` 播种出来的——页面自那一刻起就**再没变过**，而扫描一直在正常采数。
现象是「榜单看起来有数据、但永远是老的」，比整页报错难发现得多。

于是页面改读 `bot_targets`：那里每一行本来就带着自己的 `military_score_at_utc`，
正是用户要的「每条数据的更新时间」，而且是**逐屏**的真实读取时刻，不是整趟一个值。
快照表保留给 POST 接口当历史归档，不再是页面的数据源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from uuid import UUID, uuid4

from sqlalchemy import Select, UnaryExpression, func, nulls_last, select
from sqlalchemy.orm import InstrumentedAttribute, Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.domain.scan_bounds import PIRATE_POSITIONS

from . import models as orm

#: 搜索框里能写成坐标的两种形状：`bot_2_137_5` 与 `2:137:5`，允许只给到恒星系。
_COORDINATE_QUERY_RE = re.compile(r"^(?:bot[_\s]+)?(\d+)[:_\s]+(\d+)(?:[:_\s]+(\d+))?$", re.I)

#: 榜单能按哪几列排序，以及默认是哪一列。
#:
#: 用户口径（2026-08-17）：「军力榜列表增加排序功能，默认时间排序」。所以默认是
#: `observed_at` 降序——最近读到的排最前面，这也是「这张榜现在长什么样」最直接的读法。
BoardSort = Literal["observed_at", "score", "rank", "coordinate"]
BoardDirection = Literal["asc", "desc"]

#: 列表的时间窗，判据是每行自己的 `military_score_at_utc`（页面上那个「更新时间」）。
#:
#: 用户口径（2026-08-17）：「列表数据范围为 24 小时内的数据」。榜是逐屏滚出来的，
#: 库里 1,700+ 行里混着好几天前读到的旧值，不设窗就等于把「现在的榜」和「历史存档」
#: 端在同一张表里，而两者长得一模一样。
#:
#: ⚠️ **`all` 必须留着。** 排障时经常要看更早的数据——2026-08-17 晚上就因为看不到
#: 历史数据绕了路。窗口是默认值，不是牢笼。
BoardWindow = Literal["24h", "7d", "all"]
BOARD_WINDOW_HOURS: Final[dict[BoardWindow, float | None]] = {
    "24h": 24.0,
    "7d": 24.0 * 7,
    "all": None,
}

#: 坐标要三列一起排：拼成字符串排会把 `4:10:1` 排到 `4:9:1` 前面。
_BOARD_COORDINATE_COLUMNS: Final[tuple[InstrumentedAttribute[int], ...]] = (
    orm.BotTargetRow.galaxy,
    orm.BotTargetRow.system,
    orm.BotTargetRow.position,
)

#: 排序键 → 真正进 `ORDER BY` 的列。**这张表就是白名单本身。**
#:
#: 调用方传来的排序键只用来在这里查表，一个字符也不会进 SQL 文本。`ORDER BY` 的
#: 列名没法走绑定参数，所以「就拼一下」在这里等于把注入口子敞开——白名单是这一
#: 处唯一的防线，别为了多支持一列而绕开它。
#:
#: 查不到就抛错，不静默回落到默认排序：回落会让「按名次排」看起来生效了，其实
#: 一直在按别的列排，而页面上根本看不出来。
_BOARD_SORT_COLUMNS: Final[dict[str, tuple[InstrumentedAttribute[Any], ...]]] = {
    # 页面上叫「更新时间」，库里是 `military_score_at_utc`：这一行的军力值是什么
    # 时候读到的。时间窗用的也是这一列，排序和筛选说的必须是同一件事。
    "observed_at": (orm.BotTargetRow.military_score_at_utc,),
    "score": (orm.BotTargetRow.military_score,),
    "rank": (orm.BotTargetRow.military_rank,),
    "coordinate": _BOARD_COORDINATE_COLUMNS,
}


@dataclass(frozen=True)
class MilitaryRankingPage:
    snapshot_id: UUID | None
    captured_at_utc: datetime | None
    rows: tuple[RankingRow, ...]
    total: int


@dataclass(frozen=True)
class MilitaryBoardRow:
    """榜单上的一行，来自 `bot_targets` 的实时状态。"""

    rank: int | None
    name: str
    score: float
    coordinate: Coordinate
    #: **这一行是什么时候读到的。** 逐屏采集，所以同一榜上不同行的时刻可以差一小时。
    observed_at_utc: datetime | None
    #: 军力值是插值补出来的，不是实读。页面必须把它标出来——这个仓库有一条硬规矩：
    #: 猜出来的数不许长得像量出来的。
    estimated: bool
    #: `scan` = 坐标扫描核验过；`ranking` = 只从榜单名字反解，可能是合法但错误的 OCR。
    source: str


@dataclass(frozen=True)
class MilitaryBoardPage:
    rows: tuple[MilitaryBoardRow, ...]
    total: int
    #: 整张榜最近一次采到数据的时刻（**不受筛选也不受时间窗影响**），给页面顶部
    #: 那句话用。窗内一条都没命中时它仍然有值，页面就能说清「窗里 0 条、但库里
    #: 最近一次采集是某某时刻」，而不是让人以为库空了。
    refreshed_at_utc: datetime | None
    #: 时间窗的下界；`None` 表示这次没设窗（`window=all`）。页面靠它把当前范围
    #: 写清楚——「命中 N 条」不写明范围就会被当成库里的全部。
    window_start_utc: datetime | None


class MilitaryRankingRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def append_snapshot(self, rows: list[RankingRow], *, captured_at_utc: datetime) -> UUID:
        """把一次读榜的结果落库。**海盗行在这里就丢掉，不进库。**

        用户口径（2026-08-16）：「军力榜需要删除类型是 pirate 的数据，这是海盗不是
        bot」。1--4 号位是游戏固定生成的海盗，名字虽然也长成 `bot_7_495_1`，但它
        不是 bot 攻击的目标——`is_bot_coordinate` 早就把它挡在目标池外，所以它在
        榜里唯一的作用就是虚增行数、把 `kind` 筛选和「已扫多少 bot」一起算歪。

        ⚠️ **不能靠军力值把它认出来。** 2026-08-16 实测：海盗 100 行 avg 7,581、
        max 43,260；bot 1,705 行 avg 7,830、max 93,920——两者分布基本重合，海盗
        既不是榜首也不是异常值。唯一可靠的判据是位号。

        丢在入库口而不是在页面上过滤：过滤只是眼不见，下一次扫描又会写回来。
        真人行（坐标反解不出来、`coordinate is None`）不受影响——这里只挡海盗位。

        ## 每行自己的读取时刻

        用户口径（2026-08-16）：「军力榜我需要的是每条数据的更新时间」。所以
        `RankingRow.observed_at_utc` 会**逐行**写进 `observed_at_utc` 列；调用方
        没给的行回落到本次快照的 `captured_at_utc`。

        ⚠️ **回落用的是快照时刻，不是 `datetime.now()`。** 那个字段要回答的是
        「这个军力值是什么时候读到的」，而不是「这批行是什么时候插进库的」。
        入库可能发生在读完之后很久（补录、离线导入、重放一份历史 payload），
        写 `now()` 会让一条三天前读到的数据显示成刚刚更新——恰好把这个字段
        存在的意义反过来。回落到快照时刻至少仍是一个真实的读取时刻，只是精度
        退到整趟而已。
        """
        if captured_at_utc.tzinfo is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        rows = [
            row
            for row in rows
            if row.coordinate is None or row.coordinate.position not in PIRATE_POSITIONS
        ]
        snapshot_id = uuid4()
        with self._session_factory() as session:
            session.add(
                orm.MilitaryRankingSnapshotRow(
                    id=snapshot_id, captured_at_utc=captured_at_utc, row_count=len(rows)
                )
            )
            # SQLite enforces the foreign key immediately; flush the parent
            # before SQLAlchemy batches the child inserts.
            session.flush()
            for ordinal, row in enumerate(rows):
                coordinate = row.coordinate
                observed_at = row.observed_at_utc or captured_at_utc
                if observed_at.tzinfo is None:
                    raise ValueError("observed_at_utc must be timezone-aware")
                session.add(
                    orm.MilitaryRankingEntryRow(
                        snapshot_id=snapshot_id,
                        ordinal=ordinal,
                        rank=row.rank,
                        player_name=row.name,
                        score=row.score,
                        galaxy=None if coordinate is None else coordinate.galaxy,
                        system=None if coordinate is None else coordinate.system,
                        position=None if coordinate is None else coordinate.position,
                        observed_at_utc=observed_at,
                    )
                )
            session.commit()
        return snapshot_id

    def latest(
        self,
        *,
        rank_min: int | None = None,
        rank_max: int | None = None,
        score_min: float | None = None,
        score_max: float | None = None,
        galaxy: int | None = None,
        bot_only: bool = False,
        kind: str = "all",
        query: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> MilitaryRankingPage:
        with self._session_factory() as session:
            snapshot = session.scalar(
                select(orm.MilitaryRankingSnapshotRow)
                .order_by(orm.MilitaryRankingSnapshotRow.captured_at_utc.desc())
                .limit(1)
            )
            if snapshot is None:
                return MilitaryRankingPage(None, None, (), 0)
            statement = select(orm.MilitaryRankingEntryRow).where(
                orm.MilitaryRankingEntryRow.snapshot_id == snapshot.id
            )
            if rank_min is not None:
                statement = statement.where(orm.MilitaryRankingEntryRow.rank >= rank_min)
            if rank_max is not None:
                statement = statement.where(orm.MilitaryRankingEntryRow.rank <= rank_max)
            if score_min is not None:
                statement = statement.where(orm.MilitaryRankingEntryRow.score >= score_min)
            if score_max is not None:
                statement = statement.where(orm.MilitaryRankingEntryRow.score <= score_max)
            if galaxy is not None:
                statement = statement.where(orm.MilitaryRankingEntryRow.galaxy == galaxy)
            # `bot_only` 保留给旧 API 调用；新的页面用单一下拉枚举，不再把
            # 海盗（固定 1–4 位）误当成 bot。
            effective_kind = "bot" if bot_only else kind
            if effective_kind == "bot":
                statement = statement.where(
                    orm.MilitaryRankingEntryRow.galaxy.is_not(None),
                    orm.MilitaryRankingEntryRow.position.not_in(PIRATE_POSITIONS),
                )
            elif effective_kind == "pirate":
                statement = statement.where(
                    orm.MilitaryRankingEntryRow.galaxy.is_not(None),
                    orm.MilitaryRankingEntryRow.position.in_(PIRATE_POSITIONS),
                )
            elif effective_kind == "player":
                statement = statement.where(orm.MilitaryRankingEntryRow.galaxy.is_(None))
            if query and query.strip():
                statement = statement.where(
                    orm.MilitaryRankingEntryRow.player_name.contains(query.strip())
                )
            rows = list(
                session.scalars(
                    statement.order_by(
                        orm.MilitaryRankingEntryRow.rank, orm.MilitaryRankingEntryRow.ordinal
                    )
                ).all()
            )
        return MilitaryRankingPage(
            snapshot.id,
            snapshot.captured_at_utc,
            tuple(
                RankingRow(
                    rank=row.rank,
                    name=row.player_name,
                    score=row.score,
                    coordinate=(
                        None
                        if row.galaxy is None
                        else Coordinate(
                            row.galaxy,
                            _required_coordinate_part(row.system),
                            _required_coordinate_part(row.position),
                        )
                    ),
                    observed_at_utc=row.observed_at_utc,
                )
                for row in rows[offset : offset + limit]
            ),
            len(rows),
        )

    def live_board(
        self,
        *,
        rank_min: int | None = None,
        rank_max: int | None = None,
        score_min: float | None = None,
        score_max: float | None = None,
        galaxy: int | None = None,
        query: str | None = None,
        sort: str = "observed_at",
        direction: str = "desc",
        window_hours: float | None = 24.0,
        now_utc: datetime | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> MilitaryBoardPage:
        """从 `bot_targets` 读当前榜单。**在 SQL 里筛、在 SQL 里排、在 SQL 里数、在 SQL 里翻页。**

        排序和翻页都交给数据库：库里 1,700+ 行，全量查回来再在 Python 里排等于
        每翻一页都把整张表搬一遍，而且 `offset/limit` 会切在错的顺序上。

        `sort` 只能是 `_BOARD_SORT_COLUMNS` 的键，`direction` 只能是 `asc` / `desc`，
        都是**查表**而不是拼字符串（理由见 `_BOARD_SORT_COLUMNS` 的注释）。不认识
        的值抛 `ValueError`，不回落到默认排序。

        `window_hours` 按每行自己的 `military_score_at_utc` 掐时间窗，默认 24 小时；
        传 `None` 表示不设窗（见 `BOARD_WINDOW_HOURS`）。`now_utc` 只为测试能钉住
        窗口边界，生产不传，走真实时钟。

        没有 `kind` 参数，因为这张榜按构造只可能有 bot：`ranking_scan` 写库前先过
        `is_bot_coordinate`，海盗（1--4 位）和真人（名字反解不出坐标）根本进不来。
        2026-08-16 实测 1,721 条有军力值的行里，`is_bot=false` 0 条、海盗位 0 条。
        留一个永远返回空集的下拉框只会让人以为筛坏了。

        `rank_min` / `rank_max` 走 `military_rank`，而**那一列大多是空的**（实测
        1,721 行里只有 140 行有名次）——榜单名次只在少数几趟里读全过。用它筛会把
        绝大多数行滤掉，这是数据现状，不是 bug。
        """
        order = _board_order(sort, direction)
        window_start = _board_window_start(window_hours, now_utc)
        with self._session_factory() as session:
            base = select(orm.BotTargetRow).where(
                orm.BotTargetRow.military_score.is_not(None),
                orm.BotTargetRow.position.not_in(PIRATE_POSITIONS),
                # ⚠️ 拉黑的不上榜（用户口径 2026-08-27：「永久移出军力榜」）。
                # 和紧挨着那条海盗位一样是**坐标级、永久、无窗口**的排除，
                # 不是「这会儿不打他」——他压根不是 bot。
                orm.BotTargetRow.blacklisted_at_utc.is_(None),
            )
            if window_start is not None:
                # 时间窗和 `total` 是同一条语句上的两件事：计数必须也在窗内，
                # 否则页面会显示「命中 1721 条」却只列得出窗内那几十行。
                base = base.where(orm.BotTargetRow.military_score_at_utc >= window_start)
            filtered = self._narrow_board(
                base,
                rank_min=rank_min,
                rank_max=rank_max,
                score_min=score_min,
                score_max=score_max,
                galaxy=galaxy,
                query=query,
            )
            total = int(session.scalar(select(func.count()).select_from(filtered.subquery())) or 0)
            rows = session.scalars(
                filtered.order_by(*order).offset(max(offset, 0)).limit(limit)
            ).all()
            # 顶部那句「数据更新时间」说的是整张榜最近一次采集，所以不带筛选条件。
            refreshed = session.scalar(
                select(func.max(orm.BotTargetRow.military_score_at_utc)).where(
                    orm.BotTargetRow.military_score.is_not(None),
                    orm.BotTargetRow.position.not_in(PIRATE_POSITIONS),
                    # 这一句和上面 `base` 那道闸必须同进同退：拉黑的行军力值冻结在
                    # 拉黑那一刻，漏掉它「数据更新时间」会被一个不再更新的行钉死在
                    # 过去，而榜上每一行都是新的。
                    orm.BotTargetRow.blacklisted_at_utc.is_(None),
                )
            )
        return MilitaryBoardPage(
            rows=tuple(
                MilitaryBoardRow(
                    rank=row.military_rank,
                    # ⚠️ **名称从坐标推，不读 `latest_owner_name`。** 那一列是坐标扫描
                    # OCR 出来的，实测存在错读：库里 2:3:9 这一行的名字存成了
                    # `bot_2_3_3`。坐标是这一行经过核验的身份，名字不是。
                    name=f"bot_{row.galaxy}_{row.system}_{row.position}",
                    score=float(row.military_score or 0.0),
                    coordinate=Coordinate(row.galaxy, row.system, row.position),
                    observed_at_utc=row.military_score_at_utc,
                    estimated=bool(row.military_score_estimated),
                    source=row.source,
                )
                for row in rows
            ),
            total=total,
            refreshed_at_utc=refreshed,
            window_start_utc=window_start,
        )

    @staticmethod
    def _narrow_board(
        statement: Select[tuple[orm.BotTargetRow]],
        *,
        rank_min: int | None,
        rank_max: int | None,
        score_min: float | None,
        score_max: float | None,
        galaxy: int | None,
        query: str | None,
    ) -> Select[tuple[orm.BotTargetRow]]:
        if rank_min is not None:
            statement = statement.where(orm.BotTargetRow.military_rank >= rank_min)
        if rank_max is not None:
            statement = statement.where(orm.BotTargetRow.military_rank <= rank_max)
        if score_min is not None:
            statement = statement.where(orm.BotTargetRow.military_score >= score_min)
        if score_max is not None:
            statement = statement.where(orm.BotTargetRow.military_score <= score_max)
        if galaxy is not None:
            statement = statement.where(orm.BotTargetRow.galaxy == galaxy)
        return _narrow_by_query(statement, query)


def _board_order(sort: str, direction: str) -> list[UnaryExpression[Any]]:
    """把排序键翻成 `ORDER BY`。**只查表，不拼字符串。**"""
    columns = _BOARD_SORT_COLUMNS.get(sort)
    if columns is None:
        raise ValueError(f"unknown board sort key: {sort!r}")
    if direction not in ("asc", "desc"):
        raise ValueError(f"unknown board sort direction: {direction!r}")
    descending = direction == "desc"
    # NULL 一律沉底，两个方向都一样。`military_rank` 大多是空的（实测 1,721 行里
    # 只有 140 行有名次），不钉住的话「按名次排」在 PostgreSQL 上是一屏空名次打头、
    # 在 SQLite 上又是另一个样——同一个页面不该因为底下换了套库就长得不一样。
    order = [nulls_last(column.desc() if descending else column.asc()) for column in columns]
    if sort != "coordinate":
        # 平手的行要有稳定次序，否则翻页会漏行：第 2 页是重新查一次，同分的行换了
        # 个顺序，边界那几行就可能两页都不出现。坐标是这张表的唯一键。
        order.extend(nulls_last(column.asc()) for column in _BOARD_COORDINATE_COLUMNS)
    return order


def _board_window_start(window_hours: float | None, now_utc: datetime | None) -> datetime | None:
    """时间窗的下界；`None` 表示不设窗。"""
    if window_hours is None:
        return None
    now = now_utc or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now_utc must be timezone-aware")
    return now - timedelta(hours=window_hours)


def _narrow_by_query(
    statement: Select[tuple[orm.BotTargetRow]], query: str | None
) -> Select[tuple[orm.BotTargetRow]]:
    """搜索框：先当坐标解，解不出再按名字模糊匹配。

    名称是从坐标推出来的（见 `live_board`），库里没有这一列可供 `LIKE`，所以
    `bot_2_137_5` 这种输入必须走坐标解析才找得到——这也正是搜索框最常见的用法。
    允许只给到恒星系（`2:137`），那时不限位号。
    """
    text = (query or "").strip()
    if not text:
        return statement
    match = _COORDINATE_QUERY_RE.match(text)
    if match is None:
        return statement.where(orm.BotTargetRow.latest_owner_name.ilike(f"%{text}%"))
    galaxy, system, position = match.groups()
    statement = statement.where(
        orm.BotTargetRow.galaxy == int(galaxy), orm.BotTargetRow.system == int(system)
    )
    if position is not None:
        statement = statement.where(orm.BotTargetRow.position == int(position))
    return statement


def _required_coordinate_part(value: int | None) -> int:
    assert value is not None
    return value
