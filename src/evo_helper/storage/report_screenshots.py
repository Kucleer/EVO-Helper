"""战报截图的落库、回读与保留期清理。

字节直接进库的理由、以及为什么不塞进 `system_log.payload_json`，写在
`storage.models.BattleReportScreenshotRow` 上，不在这里重复。

这个模块自成一份而不是并进 `SqlAlchemyRepository`：那个类是攻击链路的账本，
每加一个方法都会被四条链路一起继承过去；截图是纯粹的旁路数据——写它失败不该
影响任何一条判据，读它只服务于一个页面上的一个链接。分开之后这条边界是**类型
层面**的，而不是靠注释提醒。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from . import models as orm

#: 保留多少天。用户口径（2026-08-17）：保留 30 天自动清理。
#:
#: 量级：实测这个 ROI 在 WEBP q90 下约 40 KB/张，每天 80 张 ≈ 3.2 MB，
#: 30 天 ≈ 97 MB。同一台 PostgreSQL 上放得下，再长就只是在攒没人会翻的历史。
DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True, slots=True)
class ReportScreenshot:
    """一张战报截图的完整回读结果。"""

    report_id: UUID
    captured_at_utc: datetime
    image_format: str
    width: int
    height: int
    image_bytes: bytes

    @property
    def media_type(self) -> str:
        """HTTP `Content-Type`。按库里记的格式填，不猜——猜错就是浏览器下载而不是显示。"""
        return f"image/{self.image_format}"


class ReportScreenshotRepository:
    """`battle_report_screenshots` 的唯一入口。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def save(
        self,
        report_id: UUID,
        *,
        image_bytes: bytes,
        width: int,
        height: int,
        captured_at_utc: datetime,
        image_format: str = "webp",
    ) -> bool:
        """存下这一份战报的截图。已经有一张时**不覆盖**，返回 False。

        不覆盖而不是报错：重复读到同一份战报是正常的（换库、回头重认），而两张
        图内容几乎一样，先到的那张同样能回答「这一发打的是谁」。报错的话，一条
        旁路数据就能把战报入库那条主路径搅黄——而这个 PR 存在的全部理由正是
        战报没能入库。

        ⚠️ **字节为空一律拒收。** 空 blob 会让页面上出现一个点开是破图的链接，
        而那比没有链接更难排查。
        """
        if not image_bytes:
            raise ValueError("screenshot bytes must not be empty")
        if captured_at_utc.tzinfo is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        with self._session_factory() as session:
            existing = session.scalar(
                select(orm.BattleReportScreenshotRow.id).where(
                    orm.BattleReportScreenshotRow.report_id == report_id
                )
            )
            if existing is not None:
                return False
            session.add(
                orm.BattleReportScreenshotRow(
                    id=uuid4(),
                    report_id=report_id,
                    captured_at_utc=captured_at_utc,
                    image_format=image_format,
                    width=width,
                    height=height,
                    byte_size=len(image_bytes),
                    image_bytes=image_bytes,
                )
            )
            session.commit()
        return True

    def load(self, report_id: UUID) -> ReportScreenshot | None:
        """按战报 id 取图；没有就 None。

        **只有这一个方法会把字节取出来。** 列表那一侧走 `has_screenshots`。
        """
        with self._session_factory() as session:
            row = session.scalar(
                select(orm.BattleReportScreenshotRow).where(
                    orm.BattleReportScreenshotRow.report_id == report_id
                )
            )
            if row is None:
                return None
            captured = row.captured_at_utc
            return ReportScreenshot(
                report_id=row.report_id,
                captured_at_utc=(
                    captured if captured.tzinfo is not None else captured.replace(tzinfo=UTC)
                ),
                image_format=row.image_format,
                width=row.width,
                height=row.height,
                image_bytes=bytes(row.image_bytes),
            )

    def has_screenshots(self, report_ids: list[UUID]) -> set[UUID]:
        """这些战报里哪几份有图。**一次查询、不取字节。**

        给列表页用。逐行调 `load` 会把几十张图（几 MB）读进内存只为判断真假。
        """
        if not report_ids:
            return set()
        with self._session_factory() as session:
            rows = session.scalars(
                select(orm.BattleReportScreenshotRow.report_id).where(
                    orm.BattleReportScreenshotRow.report_id.in_(report_ids)
                )
            ).all()
        return {UUID(str(value)) for value in rows}

    def total_bytes(self) -> int:
        """库里这些图一共占多少字节。诊断用——保留期要能先量再调。"""
        with self._session_factory() as session:
            total = session.scalar(select(func.sum(orm.BattleReportScreenshotRow.byte_size)))
        return int(total or 0)

    def purge_before(self, cutoff: datetime) -> int:
        """删掉 `captured_at_utc` 早于 `cutoff` 的图，返回删了几行。

        按**截图时刻**切而不是战报时刻：补录会把很旧的战报读进来，按战报时刻切
        的话那张图一入库就已经过期，下一次启动清理就把它删了。
        """
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must be timezone-aware")
        with self._session_factory() as session:
            result = cast(
                "CursorResult[Any]",
                session.execute(
                    delete(orm.BattleReportScreenshotRow).where(
                        orm.BattleReportScreenshotRow.captured_at_utc < cutoff
                    )
                ),
            )
            session.commit()
            return int(result.rowcount or 0)


def purge_report_screenshots(
    session_factory: sessionmaker[Session],
    *,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
) -> int:
    """删掉超过保留期的战报截图，返回删了几行。失败返回 0，不抛。

    契约照抄 `infrastructure.system_log_db.purge_system_log`，包括那两条：

    - `retention_days <= 0` 视为**不清理**。把它当成「全删」太危险，一个手滑的
      配置值就能把整批图清空。
    - 失败不抛。这个函数挂在控制台启动上，清理失败不该让控制台起不来。
    """
    if retention_days <= 0:
        return 0
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=retention_days)
    try:
        return ReportScreenshotRepository(session_factory).purge_before(cutoff)
    except Exception:  # noqa: BLE001 - 清理失败不该把控制台的启动拖垮
        return 0


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "ReportScreenshot",
    "ReportScreenshotRepository",
    "purge_report_screenshots",
]
