"""SQLAlchemy ORM models for the EVO-Helper persistence schema (plan 8.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base, UTCDateTime


class ScanPlan(Base):
    __tablename__ = "scan_plans"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    public_id: Mapped[UUID] = mapped_column(Uuid, unique=True, default=uuid4, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    time_window_start: Mapped[str] = mapped_column(String(5), default="08:00")
    time_window_end: Mapped[str] = mapped_column(String(5), default="20:00")
    timezone_name: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Fleet lines this plan may occupy, and how many stay free for the user.
    fleet_line_limit: Mapped[int] = mapped_column(Integer, default=1)
    reserved_lines: Mapped[int] = mapped_column(Integer, default=0)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at_utc: Mapped[datetime] = mapped_column(
        UTCDateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class ScanRangeRow(Base):
    __tablename__ = "scan_ranges"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    plan_id: Mapped[int] = mapped_column(ForeignKey("scan_plans.id"), index=True)
    start_galaxy: Mapped[int] = mapped_column(Integer)
    start_system: Mapped[int] = mapped_column(Integer)
    start_position: Mapped[int] = mapped_column(Integer)
    end_galaxy: Mapped[int] = mapped_column(Integer)
    end_system: Mapped[int] = mapped_column(Integer)
    end_position: Mapped[int] = mapped_column(Integer)
    origin_galaxy: Mapped[int] = mapped_column(Integer)
    origin_system: Mapped[int] = mapped_column(Integer)
    origin_position: Mapped[int] = mapped_column(Integer)
    fleet_preset_name: Mapped[str] = mapped_column(String(120))
    fleet_preset_signature: Mapped[str] = mapped_column(String(255))
    priority: Mapped[int] = mapped_column(Integer, default=0)


class RunInstance(Base):
    __tablename__ = "run_instances"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    plan_id: Mapped[int] = mapped_column(ForeignKey("scan_plans.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    target_date: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    state: Mapped[str] = mapped_column(String(32), default="DRAFT")
    cursor_galaxy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cursor_system: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cursor_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_galaxy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_system: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pending_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    drained_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 松手等待期间该睡到什么时候。持久化是关键：派出后助手不持有会话，
    #: 进程可以整个退出，恢复时靠这个字段判断现在该等还是该收。
    resume_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    #: 连续拿不到登录的次数，用于退避。拿到会话后归零。
    session_attempts: Mapped[int] = mapped_column(Integer, default=0)
    finished_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class CoordinateScanRow(Base):
    __tablename__ = "coordinate_scans"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run_instances.id"), index=True)
    galaxy: Mapped[int] = mapped_column(Integer)
    system: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    scanned_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    owner_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_artifact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)


class BotTargetRow(Base):
    __tablename__ = "bot_targets"
    __table_args__ = (
        UniqueConstraint("galaxy", "system", "position", name="uq_bot_targets_coordinate"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    galaxy: Mapped[int] = mapped_column(Integer)
    system: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    latest_owner_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_scanned_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_attack_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_dispatch_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_report_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class AttackIntentRow(Base):
    __tablename__ = "attack_intents"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "target_galaxy",
            "target_system",
            "target_position",
            "cycle_start_utc",
            "forced_revisit",
            name="uq_attack_intent_run_target_cycle",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("run_instances.id"), index=True)
    origin_galaxy: Mapped[int] = mapped_column(Integer)
    origin_system: Mapped[int] = mapped_column(Integer)
    origin_position: Mapped[int] = mapped_column(Integer)
    target_galaxy: Mapped[int] = mapped_column(Integer)
    target_system: Mapped[int] = mapped_column(Integer)
    target_position: Mapped[int] = mapped_column(Integer)
    preset_name: Mapped[str] = mapped_column(String(120))
    preset_signature: Mapped[str] = mapped_column(String(255))
    cycle_start_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    guard_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    forced_revisit: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    #: `bot` 或 `pirate`（见 `domain.records.TARGET_KIND_*`）。攻击日志按它分类。
    target_kind: Mapped[str] = mapped_column(String(16), default="bot", server_default="bot")


class AttackDispatchRow(Base):
    __tablename__ = "attack_dispatches"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("attack_intents.id"), unique=True)
    dispatched_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    dry_run: Mapped[bool] = mapped_column(Boolean)
    accepted: Mapped[bool] = mapped_column(Boolean)
    evidence_artifact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    #: 派出时读到的飞行时长，以及据此算出的预计战报时间。
    #: 助手派出后就松手，靠这个时间决定什么时候回来登录收报告。
    #: 读不到飞行时间时为 NULL——那时改为立即尝试收取，而不是无限等待。
    flight_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_report_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class BattleReportRow(Base):
    __tablename__ = "battle_reports"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    reported_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    raw_time_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attacker_origin_galaxy: Mapped[int] = mapped_column(Integer)
    attacker_origin_system: Mapped[int] = mapped_column(Integer)
    attacker_origin_position: Mapped[int] = mapped_column(Integer)
    defender_target_galaxy: Mapped[int] = mapped_column(Integer)
    defender_target_system: Mapped[int] = mapped_column(Integer)
    defender_target_position: Mapped[int] = mapped_column(Integer)
    match_status: Mapped[str] = mapped_column(String(16), default="UNMATCHED")
    match_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    manual_review_status: Mapped[str] = mapped_column(String(16), default="PENDING")
    is_from_revisit: Mapped[bool] = mapped_column(Boolean, default=False)
    ui_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: 战斗详情页的「单位」总数，双方各一。**不是**逐行明细之和——
    #: 大舰队的数量显示成 `5.36K` 这样的四舍五入值，逐行相加凑不出精确总数。
    #: 可空：早先入库的战报没有这个数，补 0 会让它看起来像「舰队为空」。
    attacker_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    defender_units: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 详情页那行大字：`VICTORY` / `FAIL`（游戏画面原文，不翻译）。
    #: 可空：这个字段之前入库的战报没读过胜负，填个值等于凭空造战果。
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: 详情页的「损失单位」总数，双方各一。海盗战报只记胜负 + 这两个数，
    #: 不写 `fleet_snapshots`（用户口径 2026-08-09，为省性能）。
    attacker_losses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    defender_losses: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatch_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("attack_dispatches.id"),
        unique=True,
        nullable=True,
    )


class FleetSnapshotRow(Base):
    __tablename__ = "fleet_snapshots"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    report_id: Mapped[UUID] = mapped_column(ForeignKey("battle_reports.id"), index=True)
    side: Mapped[str] = mapped_column(String(16))
    ship_type: Mapped[str] = mapped_column(String(64))
    count: Mapped[int] = mapped_column(Integer)
    round_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: 这一行的数没有把握。攻击判断只看总数分档，个别行不准不影响决策，
    #: 但必须让人看得出哪几行不能信。
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)


class TargetRevisitRow(Base):
    __tablename__ = "target_revisits"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    scope: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(255))
    target_galaxy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_system: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    executed_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")


class UiObservationRow(Base):
    __tablename__ = "ui_observations"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    screen: Mapped[str] = mapped_column(String(32))
    ui_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detection_result: Mapped[str | None] = mapped_column(String(255), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence_artifact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    observed_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class StateEventRow(Base):
    __tablename__ = "state_events"
    __table_args__ = (Index("ix_state_events_aggregate", "aggregate_type", "aggregate_id"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    aggregate_type: Mapped[str] = mapped_column(String(32))
    aggregate_id: Mapped[UUID] = mapped_column(Uuid)
    event: Mapped[str] = mapped_column(String(64))
    before_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    after_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    occurred_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    path: Mapped[str] = mapped_column(String(512), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(64))
    retention_policy: Mapped[str] = mapped_column(String(32), default="KEEP")
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)


class IntelFilterRow(Base):
    """A named, reusable intel query. The tree is stored as JSON text."""

    __tablename__ = "intel_filters"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(120), index=True)
    condition_tree: Mapped[str] = mapped_column(Text)
    span_start: Mapped[str | None] = mapped_column(String(16), nullable=True)
    span_end: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
