"""Filesystem-backed artifact storage and auditable UI-observation indexing."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.ports import ArtifactPayload, ArtifactRef
from evo_helper.domain.records import UiObservation
from evo_helper.storage import models as orm

_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/json": ".json",
}


class SqlAlchemyArtifactStore:
    """Write immutable evidence files and index them in the local database."""

    def __init__(self, session_factory: sessionmaker[Session], root: Path, *, source: str) -> None:
        self._session_factory = session_factory
        self._root = root.resolve()
        self._source = source

    def save(self, artifact: ArtifactPayload) -> ArtifactRef:
        if not artifact.content:
            raise ValueError("artifact content must not be empty")
        if not artifact.media_type:
            raise ValueError("artifact media_type must not be empty")
        artifact_id = uuid4()
        suffix = _SUFFIXES.get(artifact.media_type, ".bin")
        relative_path = Path("artifacts") / f"{artifact_id}{suffix}"
        target = (self._root / relative_path).resolve()
        if self._root not in target.parents:
            raise ValueError("artifact path escapes storage root")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f"{target.suffix}.tmp")
        temporary.write_bytes(artifact.content)
        temporary.replace(target)
        digest = hashlib.sha256(artifact.content).hexdigest()
        try:
            with self._session_factory() as session:
                session.add(
                    orm.ArtifactRow(
                        id=artifact_id,
                        path=relative_path.as_posix(),
                        sha256=digest,
                        media_type=artifact.media_type,
                        source=self._source,
                        retention_policy="KEEP",
                        created_at_utc=datetime.now(UTC),
                    )
                )
                session.commit()
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return ArtifactRef(artifact_id=artifact_id, path=relative_path.as_posix(), sha256=digest)


class SqlAlchemyUiObservationStore:
    """Persist vision observations only when their evidence index is valid."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def save(self, observation: UiObservation) -> None:
        if not 0.0 <= observation.confidence <= 1.0:
            raise ValueError("observation confidence must be between 0 and 1")
        if observation.observed_at_utc.tzinfo is None:
            raise ValueError("observed_at_utc must be timezone-aware")
        with self._session_factory() as session:
            if (
                observation.evidence_artifact_id is not None
                and session.scalar(
                    select(orm.ArtifactRow.id).where(
                        orm.ArtifactRow.id == observation.evidence_artifact_id
                    )
                )
                is None
            ):
                raise ValueError("UI observation references an unknown artifact")
            session.add(
                orm.UiObservationRow(
                    id=observation.observation_id,
                    screen=observation.screen,
                    ui_version=observation.ui_version,
                    detection_result=observation.detection_result,
                    confidence=observation.confidence,
                    evidence_artifact_id=observation.evidence_artifact_id,
                    observed_at_utc=observation.observed_at_utc,
                )
            )
            session.commit()
