from __future__ import annotations

import json
from pathlib import Path

from evo_helper.datasets.manifest import (
    DatasetManifest,
    compute_sha256,
    validate_capture_manifest,
    validate_manifest,
)


def _write_manifest(
    tmp_path: Path,
    *,
    is_legacy: bool = True,
    legacy_eligible: bool,
    screen: str | None = None,
) -> Path:
    sample = tmp_path / "sample.png"
    sample.write_bytes(b"fake-png-content")
    manifest = {
        "batch": "test-batch",
        "captured_at_utc": "2026-08-06T00:00:00Z",
        "sample_count": 1,
        "notes": "test",
        "samples": [
            {
                "file": "sample.png",
                "bytes": sample.stat().st_size,
                "sha256": compute_sha256(sample),
                "is_legacy": is_legacy,
                "eligible_for_current_mail_baseline": legacy_eligible,
                **({"screen": screen} if screen is not None else {}),
            }
        ],
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_manifest_round_trip_and_hash_validation(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, legacy_eligible=False)
    manifest = DatasetManifest.load(manifest_path)
    assert manifest.sample_count == 1
    assert validate_manifest(manifest, tmp_path) == []


def test_manifest_rejects_legacy_mail_baseline_pollution(tmp_path: Path) -> None:
    manifest_path = _write_manifest(tmp_path, legacy_eligible=True)
    manifest = DatasetManifest.load(manifest_path)
    errors = validate_manifest(manifest, tmp_path)
    assert any("must not enter current mail baseline" in error for error in errors)


def test_manifest_rejects_non_mail_baseline_pollution(tmp_path: Path) -> None:
    manifest_path = _write_manifest(
        tmp_path,
        is_legacy=False,
        legacy_eligible=True,
        screen="galaxy",
    )
    errors = validate_manifest(DatasetManifest.load(manifest_path), tmp_path)
    assert any("non-mail sample" in error for error in errors)


def test_capture_manifest_requires_complete_evidence_metadata(tmp_path: Path) -> None:
    manifest_path = _write_manifest(
        tmp_path,
        is_legacy=False,
        legacy_eligible=False,
        screen="home",
    )
    errors = validate_capture_manifest(DatasetManifest.load(manifest_path), tmp_path)
    assert any("capture metadata missing" in error for error in errors)


def test_capture_manifest_accepts_complete_evidence_metadata(tmp_path: Path) -> None:
    manifest_path = _write_manifest(
        tmp_path,
        is_legacy=False,
        legacy_eligible=False,
        screen="home",
    )
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["samples"][0].update(
        {
            "artifact_id": "artifact-1",
            "captured_at_utc": "2026-08-06T00:00:00Z",
            "session_id": "session-1",
            "batch": "test-batch",
            "ui_version": "unknown",
            "viewport": {"width": 1536, "height": 648, "scale": 1.0},
            "source": "root-agent-browser",
        }
    )
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    assert validate_capture_manifest(DatasetManifest.load(manifest_path), tmp_path) == []
