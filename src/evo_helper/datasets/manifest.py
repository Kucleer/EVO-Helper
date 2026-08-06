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


def compute_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
