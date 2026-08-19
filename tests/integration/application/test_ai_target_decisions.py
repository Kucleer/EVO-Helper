"""AI 选靶影子记录的存储侧：读写、保留期清理、以及 prompt 需要的那几个读法。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from evo_helper.application.ai_targeting import DEFAULT_AI_RETENTION_DAYS
from evo_helper.domain.ai_targeting import AiTargetDecision
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import MISSION_KIND_ATTACK, TARGET_KIND_BOT
from evo_helper.storage.repository import SqlAlchemyRepository

from .test_mission_scheduler import dispatch

REVISION = "61eb261c5a09"

NOW = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)

ORIGIN_A = Coordinate(4, 277, 15)
ORIGIN_B = Coordinate(9, 250, 8)
TARGET = Coordinate(4, 269, 8)


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    from support.database import scratch_database_url

    return scratch_database_url(tmp_path, "ai-target-decisions.db")


def _decision(*, status: str = "ok", decided_at: datetime = NOW) -> AiTargetDecision:
    return AiTargetDecision(
        decided_at_utc=decided_at,
        task_id=2,
        run_id=None,
        cycle_start_utc=decided_at - timedelta(days=3),
        budget=2,
        algorithm_picks_json='[{"target": "4:269:8"}]',
        ai_picks_json='[{"target": "4:269:8", "preset": "BBB"}]' if status == "ok" else None,
        overlap=1 if status == "ok" else None,
        prompt_text="prompt 原文",
        response_text="response 原文" if status == "ok" else None,
        model="test-model",
        prompt_tokens=100,
        completion_tokens=50,
        latency_ms=800,
        status=status,
        violations_json="[]",
    )


class TestMigration:
    def test_this_revision_is_the_head(self) -> None:
        """我的迁移是新链的 head：生产重启 bat 会升到它。"""
        root = Path(__file__).resolve().parents[3]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "alembic"))
        script = ScriptDirectory.from_config(config)
        assert script.get_heads() == [REVISION]
        assert REVISION in {revision.revision for revision in script.walk_revisions()}

    def test_the_new_table_and_knobs_exist_after_upgrade(self, database_url: str) -> None:
        from alembic import command

        root = Path(__file__).resolve().parents[3]
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "alembic"))
        config.set_main_option("sqlalchemy.url", database_url)
        config.attributes["database_url"] = database_url
        command.upgrade(config, REVISION)

        inspector = inspect(_engine(database_url))
        columns = {c["name"] for c in inspector.get_columns("ai_target_decisions")}
        assert {
            "id",
            "decided_at_utc",
            "task_id",
            "budget",
            "algorithm_picks_json",
            "ai_picks_json",
            "prompt_text",
            "response_text",
            "status",
        } <= columns
        knobs = {c["name"] for c in inspector.get_columns("military_attack_config")}
        assert {
            "ai_shadow_enabled",
            "ai_model",
            "ai_timeout_seconds",
            "ai_sample_size",
            "ai_retention_days",
        } <= knobs


def _engine(database_url: str):
    from sqlalchemy import create_engine

    return create_engine(database_url)


class TestDecisionStore:
    def test_save_and_recent_roundtrip(self, repository: SqlAlchemyRepository) -> None:
        repository.save_ai_target_decision(_decision())
        rows = repository.recent_ai_target_decisions(limit=10)
        assert len(rows) == 1
        row = rows[0]
        assert row.status == "ok"
        assert row.budget == 2
        assert row.prompt_text == "prompt 原文"
        assert row.response_text == "response 原文"
        assert row.violations_json == "[]"
        assert row.decided_at_utc.replace(tzinfo=UTC) == NOW

    def test_recent_is_newest_first(self, repository: SqlAlchemyRepository) -> None:
        repository.save_ai_target_decision(_decision(decided_at=NOW - timedelta(hours=1)))
        repository.save_ai_target_decision(_decision(decided_at=NOW))
        rows = repository.recent_ai_target_decisions(limit=10)
        assert [row.budget for row in rows] == [2, 2]
        assert rows[0].decided_at_utc > rows[1].decided_at_utc

    def test_purge_only_removes_rows_older_than_the_cutoff(
        self, repository: SqlAlchemyRepository
    ) -> None:
        repository.save_ai_target_decision(_decision(decided_at=NOW - timedelta(days=2)))
        repository.save_ai_target_decision(_decision(decided_at=NOW))
        purged = repository.purge_ai_target_decisions(NOW - timedelta(days=1))
        assert purged == 1
        rows = repository.recent_ai_target_decisions(limit=10)
        assert len(rows) == 1
        assert rows[0].decided_at_utc.replace(tzinfo=UTC) == NOW

    def test_a_failed_record_is_stored_without_picks(
        self, repository: SqlAlchemyRepository
    ) -> None:
        repository.save_ai_target_decision(_decision(status="invalid_json"))
        row = repository.recent_ai_target_decisions(limit=10)[0]
        assert row.status == "invalid_json"
        assert row.ai_picks_json is None


class TestPromptReadings:
    def test_inflight_lines_lists_unknown_and_known_holdings(
        self, repository: SqlAlchemyRepository, run_id: object
    ) -> None:
        # 无航线钟 → 按兜底占着，line_free_at_utc 为 None（「时长未知」档）。
        dispatch(
            repository,
            run_id,
            TARGET_KIND_BOT,
            target=Coordinate(4, 269, 8),
            dispatched_at=NOW - timedelta(minutes=10),
            origin=ORIGIN_A,
        )
        # 有航线钟、已回港 → 不占。
        dispatch(
            repository,
            run_id,
            TARGET_KIND_BOT,
            target=Coordinate(4, 270, 8),
            dispatched_at=NOW - timedelta(hours=2),
            origin=ORIGIN_A,
            flight=timedelta(minutes=20),
        )

        lines = repository.inflight_lines(now_utc=NOW, origin=ORIGIN_A, hold=timedelta(minutes=90))
        assert len(lines) == 1
        assert lines[0].line_free_at_utc is None

    def test_last_bot_attack_at_ignores_scout_and_rejected(
        self, repository: SqlAlchemyRepository, run_id: object
    ) -> None:
        from evo_helper.domain.models import FleetPresetRef
        from evo_helper.domain.records import MISSION_KIND_SCOUT, AttackDispatch, AttackIntent

        def attempt(target: Coordinate, *, at: datetime, accepted: bool, mission: str) -> None:
            intent_id, dispatch_id = uuid4(), uuid4()
            repository.save_attack_intent(
                AttackIntent(
                    intent_id=intent_id,
                    run_id=run_id,  # type: ignore[arg-type]
                    origin=ORIGIN_A,
                    target=target,
                    preset=FleetPresetRef(name="BBB", signature="sig"),
                    cycle_start_utc=at,
                    created_at_utc=at,
                    target_kind=TARGET_KIND_BOT,
                )
            )
            repository.save_dispatch(
                AttackDispatch(
                    dispatch_id=dispatch_id,
                    intent_id=intent_id,
                    dispatched_at_utc=at,
                    accepted=accepted,
                    mission_kind=mission,
                )
            )

        attempt(
            Coordinate(4, 269, 8),
            at=NOW - timedelta(hours=5),
            accepted=True,
            mission=MISSION_KIND_ATTACK,
        )
        # 侦察发不算「我方打过」。
        attempt(
            Coordinate(4, 269, 8),
            at=NOW - timedelta(hours=1),
            accepted=True,
            mission=MISSION_KIND_SCOUT,
        )
        # 被拒的不算。
        attempt(
            Coordinate(4, 270, 8),
            at=NOW - timedelta(hours=1),
            accepted=False,
            mission=MISSION_KIND_ATTACK,
        )

        result = repository.last_bot_attack_at([Coordinate(4, 269, 8), Coordinate(4, 270, 8)])
        assert result[Coordinate(4, 269, 8)] == NOW - timedelta(hours=5)
        assert result[Coordinate(4, 270, 8)] is None

    def test_protection_seen_at_returns_known_moments(
        self, session_factory, repository: SqlAlchemyRepository
    ) -> None:
        from evo_helper.storage import models as orm

        with session_factory() as session:
            session.add(
                orm.BotTargetRow(
                    id=uuid4(),
                    galaxy=ORIGIN_A.galaxy,
                    system=ORIGIN_A.system,
                    position=ORIGIN_A.position,
                    is_bot=True,
                    protection_seen_at_utc=NOW - timedelta(hours=2),
                )
            )
            session.commit()
        result = repository.bot_target_protection_seen_at([ORIGIN_A, TARGET])
        assert result[ORIGIN_A] == NOW - timedelta(hours=2)
        assert result[TARGET] is None

    def test_default_retention_is_ninety_days(self) -> None:
        assert DEFAULT_AI_RETENTION_DAYS == 90
