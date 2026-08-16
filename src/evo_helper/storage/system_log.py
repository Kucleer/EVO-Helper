"""`system_log` 的落盘、查询与清理。

查询**在 SQL 里筛、在 SQL 里数**（同 `web.persistent_service.list_planets` 的
口径）：这张表按设计会长到几十万行，把它全查出来再在 Python 里过滤，既慢又会
诱使页面拿「本页行数」冒充总数——那正是星球列表当年把「扫描停在 2:32」这个
假象显示出来的原因。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, CursorResult, delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.infrastructure.system_log import SystemLogRecord

from . import models as orm

#: 一页最多多少行。页面默认 200；上限挡的是手改链接要 100 万行把控制台拖死。
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 200


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

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

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
    ) -> SystemLogPage:
        """按条件取一页，**倒序**（最新在上）。

        排序带上 `id` 而不是只按 `logged_at_utc`：批量刷盘会让同一毫秒里落进
        好几条，只按时刻排的话相同时刻之间的顺序由数据库自己决定，翻页时同一行
        可能在第 1 页和第 2 页各出现一次、另一行一次都不出现。
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
        with self._session_factory() as session:
            statement = select(orm.SystemLogRow)
            for clause in clauses:
                statement = statement.where(clause)
            total = int(session.scalar(select(func.count()).select_from(statement.subquery())) or 0)
            rows = session.scalars(
                statement.order_by(
                    orm.SystemLogRow.logged_at_utc.desc(), orm.SystemLogRow.id.desc()
                )
                .offset(offset)
                .limit(limit)
            ).all()
            hosts = tuple(
                str(value)
                for value in session.scalars(
                    select(orm.SystemLogRow.host).distinct().order_by(orm.SystemLogRow.host)
                ).all()
            )
            sources = tuple(
                str(value)
                for value in session.scalars(
                    select(orm.SystemLogRow.source).distinct().order_by(orm.SystemLogRow.source)
                ).all()
            )
        return SystemLogPage(
            rows=tuple(_entry(row) for row in rows),
            total=total,
            offset=offset,
            limit=limit,
            hosts=hosts,
            sources=sources,
        )

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


def _entry(row: orm.SystemLogRow) -> SystemLogEntry:
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
        payload_json=row.payload_json,
    )


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "SystemLogEntry",
    "SystemLogPage",
    "SystemLogRepository",
]
