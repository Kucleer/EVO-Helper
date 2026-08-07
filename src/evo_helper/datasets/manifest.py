"""Manifest loading and integrity validation for vision datasets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SampleEntry:
    file: str
    bytes: int
    sha256: str
    is_legacy: bool
    eligible_for_current_mail_baseline: bool
    screen: str | None = None
    artifact_id: str | None = None
    captured_at_utc: str | None = None
    session_id: str | None = None
    batch: str | None = None
    ui_version: str | None = None
    viewport: dict[str, object] | None = None
    source: str | None = None


@dataclass(frozen=True)
class DatasetManifest:
    batch: str
    captured_at_utc: str
    sample_count: int
    notes: str
    samples: tuple[SampleEntry, ...] = field(default_factory=tuple)

    @classmethod
    def load(cls, path: Path | str) -> DatasetManifest:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        samples = tuple(
            SampleEntry(
                file=str(entry["file"]),
                bytes=int(entry["bytes"]),
                sha256=str(entry["sha256"]),
                is_legacy=bool(entry["is_legacy"]),
                eligible_for_current_mail_baseline=bool(
                    entry.get("eligible_for_current_mail_baseline", False)
                ),
                screen=str(entry["screen"]) if entry.get("screen") is not None else None,
                artifact_id=str(entry["artifact_id"])
                if entry.get("artifact_id") is not None
                else None,
                captured_at_utc=str(entry["captured_at_utc"])
                if entry.get("captured_at_utc") is not None
                else None,
                session_id=str(entry["session_id"])
                if entry.get("session_id") is not None
                else None,
                batch=str(entry["batch"]) if entry.get("batch") is not None else None,
                ui_version=str(entry["ui_version"])
                if entry.get("ui_version") is not None
                else None,
                viewport=dict(entry["viewport"])
                if isinstance(entry.get("viewport"), dict)
                else None,
                source=str(entry["source"]) if entry.get("source") is not None else None,
            )
            for entry in raw.get("samples", [])
        )
        return cls(
            batch=str(raw["batch"]),
            captured_at_utc=str(raw["captured_at_utc"]),
            sample_count=int(raw["sample_count"]),
            notes=str(raw.get("notes", "")),
            samples=samples,
        )


def validate_manifest(manifest: DatasetManifest, base_dir: Path | str) -> list[str]:
    """Return a list of violations; an empty list means the manifest is valid."""
    errors: list[str] = []
    root = Path(base_dir)
    if manifest.sample_count != len(manifest.samples):
        errors.append(f"sample_count {manifest.sample_count} != {len(manifest.samples)} entries")
    seen: set[str] = set()
    for entry in manifest.samples:
        if entry.file in seen:
            errors.append(f"duplicate sample file: {entry.file}")
        seen.add(entry.file)
        path = root / entry.file
        if not path.is_file():
            errors.append(f"missing sample file: {entry.file}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry.sha256:
            errors.append(f"sha256 mismatch for {entry.file}: {digest}")
        if entry.is_legacy and entry.eligible_for_current_mail_baseline:
            errors.append(f"legacy sample must not enter current mail baseline: {entry.file}")
        if entry.eligible_for_current_mail_baseline and entry.screen != "mail_list":
            errors.append(f"non-mail sample must not enter current mail baseline: {entry.file}")
    return errors


def validate_capture_manifest(manifest: DatasetManifest, base_dir: Path | str) -> list[str]:
    """Validate complete evidence metadata for a live browser capture batch."""
    errors = validate_manifest(manifest, base_dir)
    required = (
        "artifact_id",
        "captured_at_utc",
        "session_id",
        "batch",
        "screen",
        "ui_version",
        "viewport",
        "source",
    )
    seen_artifacts: set[str] = set()
    for entry in manifest.samples:
        for field_name in required:
            value = getattr(entry, field_name)
            if value is None or value == "":
                errors.append(f"capture metadata missing {field_name}: {entry.file}")
        if entry.artifact_id is not None:
            if entry.artifact_id in seen_artifacts:
                errors.append(f"duplicate artifact_id: {entry.artifact_id}")
            seen_artifacts.add(entry.artifact_id)
        if entry.batch is not None and entry.batch != manifest.batch:
            errors.append(f"sample batch mismatch: {entry.file}")
        if entry.viewport is not None:
            width = entry.viewport.get("width")
            height = entry.viewport.get("height")
            scale = entry.viewport.get("scale")
            if not all(
                isinstance(value, (int, float)) and value > 0 for value in (width, height, scale)
            ):
                errors.append(f"invalid viewport metadata: {entry.file}")
    return errors


def compute_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
