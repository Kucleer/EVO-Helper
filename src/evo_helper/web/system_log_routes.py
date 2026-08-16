"""系统日志的查询接口与页面。

⚠️ **不要和 `/logs` 搞混。** `/logs` 是「攻击日志」——每一发打出去的舰队，读的是
`attack_intents ⟕ attack_dispatches ⟕ battle_reports`。这一页读的是 `system_log`，
也就是实机脚本与控制台自己的诊断输出。两者的行完全不是一回事，共用一个路由会让
「哪一页能看到 runner 报的错」这件事永远说不清。

筛选与翻页全部走查询参数、全部下推到 SQL（口径同 `persistent_service.list_planets`）：
这张表按设计会长到几十万行，取回一批再在 Python 里挑等于先砍掉历史再问历史。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.infrastructure.system_log import LEVELS
from evo_helper.storage.system_log import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    SystemLogPage,
    SystemLogRepository,
)

#: 页面上的任务链路下拉。与 `mission_kind` 列存的取值一套。
MISSION_KINDS = ("pirate", "bot", "scan", "ranking")

PAGE_SIZES = (50, 200, 500, 1000)


def _blank(value: str | None) -> str | None:
    """空串一律当「没填」。

    每个下拉框的「全部」那一项 value 就是空串，浏览器提交表单必然把
    `level=&host=` 都带上。当成「等于空字符串」去查，默认视图点下去永远 0 条。
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_moment(value: str | None) -> datetime | None:
    """把 `<input type="datetime-local">` 的值读成 UTC 时刻。

    浏览器交上来的是不带时区的本地挂钟（`2026-08-16T09:30`）。这一页的时间列
    显示的是 UTC，两个框也按 UTC 解释——**同一页里两套时区**比时区不方便糟得多。
    解析不了就当没填：一页 422 的 JSON 读起来就是「控制台坏了」。
    """
    text = _blank(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _parse_run_id(value: str | None) -> UUID | None:
    text = _blank(value)
    if text is None:
        return None
    try:
        return UUID(text)
    except ValueError:
        return None


def _row_out(entry: Any) -> dict[str, Any]:
    return {
        "id": entry.id,
        "logged_at_utc": entry.logged_at_utc,
        "level": entry.level,
        "source": entry.source,
        "host": entry.host,
        "pid": entry.pid,
        "run_id": None if entry.run_id is None else str(entry.run_id),
        "task_id": entry.task_id,
        "mission_kind": entry.mission_kind,
        "message": entry.message,
        "payload_json": entry.payload_json,
    }


def register_system_log_routes(app: FastAPI, session_factory: sessionmaker[Session]) -> None:
    repository = SystemLogRepository(session_factory)
    router = APIRouter(tags=["system-log"])
    templates: Jinja2Templates = app.state.templates

    def _page(
        *,
        level: str | None,
        source: str | None,
        host: str | None,
        mission_kind: str | None,
        run_id: str | None,
        since: str | None,
        until: str | None,
        q: str | None,
        offset: int,
        limit: int,
    ) -> SystemLogPage:
        return repository.query(
            level=_blank(level),
            source=_blank(source),
            host=_blank(host),
            mission_kind=_blank(mission_kind),
            run_id=_parse_run_id(run_id),
            since=_parse_moment(since),
            until=_parse_moment(until),
            keyword=_blank(q),
            offset=offset,
            limit=limit,
        )

    @router.get("/api/system-log")
    async def api_system_log(
        level: str | None = None,
        source: str | None = None,
        host: str | None = None,
        mission_kind: str | None = None,
        run_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        q: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    ) -> dict[str, Any]:
        page = _page(
            level=level,
            source=source,
            host=host,
            mission_kind=mission_kind,
            run_id=run_id,
            since=since,
            until=until,
            q=q,
            offset=offset,
            limit=limit,
        )
        return {
            "total": page.total,
            "offset": page.offset,
            "limit": page.limit,
            "has_more": page.has_more,
            "hosts": list(page.hosts),
            "sources": list(page.sources),
            "rows": [_row_out(row) for row in page.rows],
        }

    @router.get("/system-log", response_class=HTMLResponse, include_in_schema=False)
    async def system_log_page(
        request: Request,
        level: str | None = None,
        source: str | None = None,
        host: str | None = None,
        mission_kind: str | None = None,
        run_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        q: str | None = None,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> HTMLResponse:
        """系统日志页。筛选与翻页都在链接里，可收藏、可分享、后退键能用。

        `offset` / `limit` 这里不声明成会 422 的类型：手改链接写出
        `?limit=` 是常事，而一页 JSON 报错读起来就是「控制台坏了」。
        """
        offset = max(offset, 0)
        limit = min(max(limit, 1), MAX_PAGE_SIZE)
        page = _page(
            level=level,
            source=source,
            host=host,
            mission_kind=mission_kind,
            run_id=run_id,
            since=since,
            until=until,
            q=q,
            offset=offset,
            limit=limit,
        )

        def page_url(new_offset: int) -> str:
            params = {
                "level": _blank(level) or "",
                "source": _blank(source) or "",
                "host": _blank(host) or "",
                "mission_kind": _blank(mission_kind) or "",
                "run_id": _blank(run_id) or "",
                "since": _blank(since) or "",
                "until": _blank(until) or "",
                "q": _blank(q) or "",
                "limit": str(limit),
                "offset": str(new_offset),
            }
            return "/system-log?" + urlencode({k: v for k, v in params.items() if v})

        return templates.TemplateResponse(
            request=request,
            name="system_log.html",
            context={
                "active": "system-log",
                "page": page,
                "levels": LEVELS,
                "mission_kinds": MISSION_KINDS,
                "page_sizes": PAGE_SIZES,
                "level_value": _blank(level) or "",
                "source_value": _blank(source) or "",
                "host_value": _blank(host) or "",
                "mission_kind_value": _blank(mission_kind) or "",
                "run_id_value": _blank(run_id) or "",
                "since_value": _blank(since) or "",
                "until_value": _blank(until) or "",
                "q_value": _blank(q) or "",
                # 认不出来的 run_id / 时刻要说出来，否则「没按它筛」是悄悄发生的，
                # 而用户会把下面那些行当成筛出来的结果。
                "ignored": _ignored(run_id, since, until),
                "prev_url": page_url(max(offset - limit, 0)) if offset > 0 else None,
                "next_url": page_url(offset + limit) if page.has_more else None,
            },
        )

    app.include_router(router)


def _ignored(run_id: str | None, since: str | None, until: str | None) -> list[str]:
    notes: list[str] = []
    if _blank(run_id) and _parse_run_id(run_id) is None:
        notes.append("run_id 不是合法 UUID，这一页没有按它筛")
    if _blank(since) and _parse_moment(since) is None:
        notes.append("起始时刻认不出来，这一页没有按它筛")
    if _blank(until) and _parse_moment(until) is None:
        notes.append("结束时刻认不出来，这一页没有按它筛")
    return notes


__all__ = ["MISSION_KINDS", "PAGE_SIZES", "register_system_log_routes"]
