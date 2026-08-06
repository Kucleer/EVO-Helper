from __future__ import annotations

import json
from pathlib import Path

from evo_helper.datasets.manifest import DatasetManifest, compute_sha256, validate_manifest


def _write_manifest(tmp_path: Path, *, legacy_eligible: bool) -> Path:
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
                "is_legacy": True,
                "eligible_for_current_mail_baseline": legacy_eligible,
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
