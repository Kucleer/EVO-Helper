"""SQLAlchemy ORM models for the EVO-Helper persistence schema (plan 8.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, UniqueConstraint, Uuid
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
    started_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    drained_at_utc: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
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


class AttackDispatchRow(Base):
    __tablename__ = "attack_dispatches"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    intent_id: Mapped[UUID] = mapped_column(ForeignKey("attack_intents.id"), unique=True)
    dispatched_at_utc: Mapped[datetime] = mapped_column(UTCDateTime)
    dry_run: Mapped[bool] = mapped_column(Boolean)
    accepted: Mapped[bool] = mapped_column(Boolean)
    evidence_artifact_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)


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
