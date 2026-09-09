"""`system_log` 的落盘、查询与清理。

查询**在 SQL 里筛、在 SQL 里数**（同 `web.persistent_service.list_planets` 的
口径）：这张表按设计会长到几十万行，把它全查出来再在 Python 里过滤，既慢又会
诱使页面拿「本页行数」冒充总数——那正是星球列表当年把「扫描停在 2:32」这个
假象显示出来的原因。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, CursorResult, case, delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.infrastructure.system_log import (
    SCREENSHOT_PAYLOAD_KEYS,
    SystemLogRecord,
    screenshot_base64,
)

from . import models as orm

#: 一页最多多少行。页面默认 200；上限挡的是手改链接要 100 万行把控制台拖死。
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 200

#: 页面上那一列愿意内联多少字符的 `payload_json`；超过就只报大小，正文点开再取。
#:
#: ⚠️ **这不是偏好项，是按库里的分布量出来的**（生产库 2026-09-09，16.4 万行）：
#: payload 长度 p50=2、p90=320、p99=458 字符，而超过 4096 的有 836 行——
#: 4096 到 8192 之间**一行都没有**，取值落在这个空档里。那 836 行里 835 行大是因为
#: 夹了一张现场图的 base64（8 万到 16 万字符），它们占 payload 总体积的 89.8%。
#:
#: 代价是这 0.5% 的行在列表页上要多点一下才看得到 payload 文字；收益是一页
#: 落在这些行上时，服务端从 3.9 秒降到 0.11 秒（实测同上，200 行合计 27 MB）。
PAYLOAD_INLINE_LIMIT = 4096

#: 两个筛选下拉框的候选值缓存多久。
#:
#: ⚠️ **调这个值不会让任何取值「查不到」。** 新机器写下第一条日志时，那一条就是
#: 最新的一行，而当前这一页里出现过的 host / source 一律并进候选（见
#: `_facets`）——所以默认视图上它**当场**就在下拉框里。这个 TTL 只决定两件次要的
#: 事：一台不再写日志的机器多久从框里消失，以及某个取值只存在于**当前筛选之外**
#: 时（比如在按 level 筛的页面上）多久补进来。所以它是常量，不是运维旋钮。
FACET_CACHE_TTL_S = 60.0


@dataclass(frozen=True, slots=True)
class SystemLogEntry:
    id: int
    logged_at_utc: datetime
    level: str
    source: str
    host: str
    pid: int
    message: str
    run_id: UUID | None
    task_id: int | None
    mission_kind: str | None
    payload_json: str
    #: 库里那段 `payload_json` 有多长。**按库里的长度算，不是按上面这个字段**——
    #: 超限时上面那个是空的，而页面要说得出「省下来的是多大一段」。
    payload_bytes: int = 0
    #: 这一条有没有现场图。取到 payload 时是**确切**的（值为空串的键不算图），
    #: 超限没取时退化成「payload 里提到过那个键」。
    has_screenshot: bool = False

    @property
    def payload_withheld(self) -> bool:
        """库里有 payload 但这一次没取回来——页面据此把它渲染成一个链接。"""
        return self.payload_bytes > 0 and not self.payload_json


@dataclass(frozen=True, slots=True)
class SystemLogPage:
    rows: tuple[SystemLogEntry, ...]
    total: int
    offset: int
    limit: int
    #: 筛选之外的全局事实，给页面上的下拉框用。
    hosts: tuple[str, ...]
    sources: tuple[str, ...]

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.rows) < self.total


class SystemLogRepository:
    """`system_log` 的唯一入口。写入侧只有 `append` 一个方法，供 sink 调用。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        #: 下拉框候选值的缓存。`clock` 只为让「过期之后重新取」可测——
        #: 真正的写入方在另一台机器上，没法靠「本进程写过」去作废。
        self._clock = clock
        self._facets_fetched_at: float | None = None
        self._cached_hosts: tuple[str, ...] = ()
        self._cached_sources: tuple[str, ...] = ()

    # -- 写入 ---------------------------------------------------------------

    def append(self, records: Sequence[SystemLogRecord]) -> None:
        """一批一次事务。**异常照抛**——吞异常是 sink 的职责，不是这里的。

        分工写死在这里：仓储把失败如实报上去，`SystemLogSink._write_batch`
        才是那个「一条都不许漏出去」的边界。两边都吞的话，写库其实一直在失败
        这件事就再也没人知道了。
        """
        if not records:
            return
        with self._session_factory() as session:
            session.add_all(
                [
                    orm.SystemLogRow(
                        logged_at_utc=record.logged_at_utc,
                        level=record.level,
                        source=record.source,
                        host=record.host,
                        pid=record.pid,
                        message=record.message,
                        run_id=record.run_id,
                        task_id=record.task_id,
                        mission_kind=record.mission_kind,
                        payload_json=record.payload_json,
                    )
                    for record in records
                ]
            )
            session.commit()

    # -- 读取 ---------------------------------------------------------------

    def query(
        self,
        *,
        level: str | None = None,
        source: str | None = None,
        host: str | None = None,
        mission_kind: str | None = None,
        run_id: UUID | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        keyword: str | None = None,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
        payload_inline_limit: int | None = None,
    ) -> SystemLogPage:
        """按条件取一页，**倒序**（最新在上）。

        排序带上 `id` 而不是只按 `logged_at_utc`：批量刷盘会让同一毫秒里落进
        好几条，只按时刻排的话相同时刻之间的顺序由数据库自己决定，翻页时同一行
        可能在第 1 页和第 2 页各出现一次、另一行一次都不出现。

        `payload_inline_limit` 给的是「愿意随这一页搬回来多少字符的 payload」。
        默认 `None` = 全量，`GET /api/system-log` 走的就是这条——**它必须能取到
        整段 payload**，那是排障的命根子。页面那一路传上限：超限的行只带回长度和
        「有没有现场图」，正文与图各自点开再取。

        ⚠️ 差别不是「省了几毫秒」：一页 200 行全落在带图的行上时，全量是 27 MB /
        3.9 秒，只带长度是 0.11 秒（生产库实测 2026-09-09）。而 `length()` 与
        `LIKE` 跟「根本不碰这一列」几乎同价（45 ms vs 49 ms）——贵的是把字节
        搬过网络，不是在库里读它。
        """
        limit = min(max(limit, 1), MAX_PAGE_SIZE)
        offset = max(offset, 0)
        clauses = self._clauses(
            level=level,
            source=source,
            host=host,
            mission_kind=mission_kind,
            run_id=run_id,
            since=since,
            until=until,
            keyword=keyword,
        )
        payload_length = func.length(orm.SystemLogRow.payload_json)
        payload_column: Any = orm.SystemLogRow.payload_json
        if payload_inline_limit is not None:
            # `CASE` 挡在 SELECT 列上：超限的行返回 NULL，那几万字符**根本不上网络**。
            payload_column = case(
                (payload_length <= payload_inline_limit, orm.SystemLogRow.payload_json),
                else_=None,
            )
        with self._session_factory() as session:
            statement = select(orm.SystemLogRow.id)
            for clause in clauses:
                statement = statement.where(clause)
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            # ⚠️ **先用一个子查询把这一页的 `id` 定下来，再去投影那几列。**
            # 直接把 `length()` / `LIKE` 写进带 `OFFSET` 的那条 SELECT 的话，
            # PostgreSQL 会**对扫过的每一行**求值，而不是只对活下来的 200 行：
            # offset=100000 时那是 10 万次，实测 403 ms（换成下面这种写法 52 ms，
            # 生产库 2026-09-09）。裸列没这个问题，所以原先看不出来——
            # 表达式列是这一版新加的。
            page_ids = (
                statement.order_by(
                    orm.SystemLogRow.logged_at_utc.desc(), orm.SystemLogRow.id.desc()
                )
                .offset(offset)
                .limit(limit)
                .subquery("page_ids")
            )
            page = select(
                orm.SystemLogRow.id,
                orm.SystemLogRow.logged_at_utc,
                orm.SystemLogRow.level,
                orm.SystemLogRow.source,
                orm.SystemLogRow.host,
                orm.SystemLogRow.pid,
                orm.SystemLogRow.message,
                orm.SystemLogRow.run_id,
                orm.SystemLogRow.task_id,
                orm.SystemLogRow.mission_kind,
                payload_column.label("payload_json"),
                payload_length.label("payload_bytes"),
                _mentions_screenshot().label("mentions_screenshot"),
            ).join(page_ids, orm.SystemLogRow.id == page_ids.c.id)
            rows = session.execute(
                page.order_by(orm.SystemLogRow.logged_at_utc.desc(), orm.SystemLogRow.id.desc())
            ).all()
            entries = tuple(_entry(row) for row in rows)
            hosts, sources = self._facets(session, entries)
        return SystemLogPage(
            rows=entries,
            total=total,
            offset=offset,
            limit=limit,
            hosts=hosts,
            sources=sources,
        )

    def entry(self, entry_id: int) -> SystemLogEntry | None:
        """按 `id` 取一条，**payload 一律全量**。

        列表页刻意不搬那几万字符的现场图（见 `query` 的 `payload_inline_limit`），
        所以「点开再取」必须有这么一个按行寻址的入口——否则省下来的字节就等于
        丢掉的证据。
        """
        with self._session_factory() as session:
            row = session.execute(
                select(
                    orm.SystemLogRow.id,
                    orm.SystemLogRow.logged_at_utc,
                    orm.SystemLogRow.level,
                    orm.SystemLogRow.source,
                    orm.SystemLogRow.host,
                    orm.SystemLogRow.pid,
                    orm.SystemLogRow.message,
                    orm.SystemLogRow.run_id,
                    orm.SystemLogRow.task_id,
                    orm.SystemLogRow.mission_kind,
                    orm.SystemLogRow.payload_json.label("payload_json"),
                    func.length(orm.SystemLogRow.payload_json).label("payload_bytes"),
                    _mentions_screenshot().label("mentions_screenshot"),
                ).where(orm.SystemLogRow.id == entry_id)
            ).first()
        return None if row is None else _entry(row)

    def _facets(
        self, session: Session, entries: Sequence[SystemLogEntry]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """两个下拉框的候选值：缓存的全局 `distinct` **并上**这一页里见到的取值。

        ⚠️ **这两个 `distinct` 原本是整页最贵的一项**：各自全表扫一遍，只为答出
        2 个 host 和 17 个 source，实测占服务端每页 360 ms 里的 276 ms
        （生产库 2026-09-09）。`host` 那一列有索引也没用——btree 上没有 skip
        scan，取 distinct 照样得把 16 万条走完。

        **并上当前这一页**不是锦上添花，是这个缓存能成立的前提：写日志的机器在
        另一台上，本进程作废不了缓存，而一台新机器写下的第一条就是最新的那一行。
        并集只会让候选**变多**，绝不会少——少一个 host 就等于一台机器的日志
        「查不到了」。
        """
        now = self._clock()
        if self._facets_fetched_at is None or now - self._facets_fetched_at >= FACET_CACHE_TTL_S:
            self._cached_hosts = tuple(
                str(value)
                for value in session.scalars(
                    select(orm.SystemLogRow.host).distinct().order_by(orm.SystemLogRow.host)
                ).all()
            )
            self._cached_sources = tuple(
                str(value)
                for value in session.scalars(
                    select(orm.SystemLogRow.source).distinct().order_by(orm.SystemLogRow.source)
                ).all()
            )
            self._facets_fetched_at = now
        hosts = sorted({*self._cached_hosts, *(entry.host for entry in entries)})
        sources = sorted({*self._cached_sources, *(entry.source for entry in entries)})
        return tuple(hosts), tuple(sources)

    def recent_messages(self, *, starts_with: str, limit: int) -> list[str]:
        """最近若干条正文以 `starts_with` 开头的日志正文，**新的在前**。

        这张表按设计会长到几十万行，所以前缀匹配、排序、取前 N 全在 SQL 里做
        （同 `query`）；把全表拉回 Python 再过滤只会把控制台拖死。

        `startswith(..., autoescape=True)` 不能省：前缀里要是带了 `%` 或 `_`，
        不转义就变成通配符，匹配范围会静悄悄扩大。
        """
        if limit < 1:
            return []
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.SystemLogRow.message)
                .where(orm.SystemLogRow.message.startswith(starts_with, autoescape=True))
                .order_by(orm.SystemLogRow.logged_at_utc.desc(), orm.SystemLogRow.id.desc())
                .limit(limit)
            ).all()
        return [str(row) for row in rows]

    @staticmethod
    def _clauses(
        *,
        level: str | None,
        source: str | None,
        host: str | None,
        mission_kind: str | None,
        run_id: UUID | None,
        since: datetime | None,
        until: datetime | None,
        keyword: str | None,
    ) -> list[ColumnElement[bool]]:
        """把筛选条件译成 SQL。空串一律当「不筛」。

        空串等于不筛是硬要求：页面上每个下拉框的「全部」那一项 value 就是空串，
        浏览器提交表单必然把 `level=&host=` 带上，当成「等于空字符串」去查，
        默认视图点下去就永远是 0 条（同 `web.app` 里 `BlankableStr` 那条教训）。
        """
        clauses: list[ColumnElement[bool]] = []
        if level and level.strip():
            clauses.append(orm.SystemLogRow.level == level.strip().upper())
        if source and source.strip():
            clauses.append(orm.SystemLogRow.source == source.strip())
        if host and host.strip():
            clauses.append(orm.SystemLogRow.host == host.strip())
        if mission_kind and mission_kind.strip():
            clauses.append(orm.SystemLogRow.mission_kind == mission_kind.strip().lower())
        if run_id is not None:
            clauses.append(orm.SystemLogRow.run_id == run_id)
        if since is not None:
            clauses.append(orm.SystemLogRow.logged_at_utc >= since)
        if until is not None:
            clauses.append(orm.SystemLogRow.logged_at_utc <= until)
        if keyword and keyword.strip():
            # 关键字同时扫正文与 payload：坐标、预设名这些常常只出现在 payload 里，
            # 只搜正文的话「查 2:137 那一次派遣」会一条都搜不到。
            pattern = f"%{keyword.strip()}%"
            clauses.append(
                or_(
                    orm.SystemLogRow.message.ilike(pattern),
                    orm.SystemLogRow.payload_json.ilike(pattern),
                )
            )
        return clauses

    # -- 清理 ---------------------------------------------------------------

    def purge_before(self, cutoff: datetime) -> int:
        """删掉 `logged_at_utc` 早于 `cutoff` 的行，返回删了几行。

        按**产生时刻**切而不是入库时刻：入库时刻这张表根本没存，而且批量刷盘
        会把它推后——按它切等于让保留期随网络状况浮动。
        """
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        with self._session_factory() as session:
            # `Session.execute` 的静态返回类型是 `Result`，只有 DML 真正跑出来的
            # `CursorResult` 上才有 `rowcount`。这里明确断言，而不是把返回值改成
            # None——「删了几行」是保留策略用例唯一能断言的东西。
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    delete(orm.SystemLogRow).where(orm.SystemLogRow.logged_at_utc < cutoff)
                ),
            )
            session.commit()
            return int(result.rowcount or 0)


def _mentions_screenshot() -> ColumnElement[bool]:
    """SQL 侧的「这一段 payload 里提到过现场图那个键」。

    ⚠️ 它只是**超限没取回 payload 时**的退路，比 `screenshot_base64` 粗：键在而值
    是空串也会算命中。取回了 payload 的行一律用后者重新判（见 `_entry`），所以
    这份粗判只落在库里那 0.5% 的大行上——而那些行大就是因为真夹着一张图
    （生产库 2026-09-09：超过 4096 字符的 836 行里 835 行确实带图）。

    `contains(..., autoescape=True)` 不能省：键名里带下划线，不转义就成了
    单字符通配符，匹配范围会静悄悄扩大。
    """
    return or_(
        *(
            orm.SystemLogRow.payload_json.contains(key, autoescape=True)
            for key in SCREENSHOT_PAYLOAD_KEYS
        )
    )


def _entry(row: Any) -> SystemLogEntry:
    """一行查询结果译成 `SystemLogEntry`。

    `payload_json` 为 NULL 意味着**这一次没取**（超过了 `payload_inline_limit`），
    不是「库里没有」——两者靠 `payload_bytes` 分得开，页面据此渲染成一个链接。
    """
    payload_json = row.payload_json or ""
    # 取到 payload 就按内容确切地判，只有没取到才退回 SQL 那个粗标记。
    has_screenshot = (
        bool(screenshot_base64(payload_json)) if payload_json else bool(row.mentions_screenshot)
    )
    return SystemLogEntry(
        id=row.id,
        logged_at_utc=row.logged_at_utc,
        level=row.level,
        source=row.source,
        host=row.host,
        pid=row.pid,
        message=row.message,
        run_id=row.run_id,
        task_id=row.task_id,
        mission_kind=row.mission_kind,
        payload_json=payload_json,
        payload_bytes=int(row.payload_bytes or 0),
        has_screenshot=has_screenshot,
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "FACET_CACHE_TTL_S",
    "MAX_PAGE_SIZE",
    "PAYLOAD_INLINE_LIMIT",
    "SystemLogEntry",
    "SystemLogPage",
    "SystemLogRepository",
]
