from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from evo_helper.domain.ports import ArtifactPayload
from evo_helper.domain.records import UiObservation
from evo_helper.infrastructure.artifacts import (
    SqlAlchemyArtifactStore,
    SqlAlchemyUiObservationStore,
)
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from support.database import scratch_database_url


def test_artifact_and_ui_observation_are_persisted(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    artifact = SqlAlchemyArtifactStore(session_factory, tmp_path, source="root-agent-browser").save(
        ArtifactPayload(media_type="image/png", content=b"capture-bytes")
    )
    SqlAlchemyUiObservationStore(session_factory).save(
        UiObservation(
            observation_id=uuid4(),
            screen="mail_list",
            ui_version="unknown",
            detection_result="current mail list",
            confidence=0.99,
            observed_at_utc=datetime(2026, 8, 6, tzinfo=UTC),
            evidence_artifact_id=artifact.artifact_id,
        )
    )

    assert (tmp_path / artifact.path).read_bytes() == b"capture-bytes"
    with session_factory() as session:
        stored_artifact = session.get(orm.ArtifactRow, artifact.artifact_id)
        observation = session.scalar(select(orm.UiObservationRow))
    assert stored_artifact is not None
    assert stored_artifact.sha256 == artifact.sha256
    assert observation is not None
    assert observation.evidence_artifact_id == artifact.artifact_id


def test_ui_observation_rejects_unknown_artifact(tmp_path: Path) -> None:
    session_factory = _session_factory(tmp_path)
    observation = UiObservation(
        observation_id=uuid4(),
        screen="unknown",
        ui_version=None,
        detection_result=None,
        confidence=0.0,
        observed_at_utc=datetime(2026, 8, 6, tzinfo=UTC),
        evidence_artifact_id=uuid4(),
    )

    with pytest.raises(ValueError, match="unknown artifact"):
        SqlAlchemyUiObservationStore(session_factory).save(observation)


def _session_factory(tmp_path: Path):
    engine = create_database_engine(scratch_database_url(tmp_path, "artifacts.db"))
    Base.metadata.create_all(engine)
    return create_session_factory(engine)
