from __future__ import annotations

import json

from evo_helper.datasets.manifest import DatasetManifest, validate_manifest
from evo_helper.tools.capture import main as capture_main


def test_capture_cli_writes_manifest(tmp_path) -> None:
    out = tmp_path / "out"
    exit_code = capture_main(
        [
            "--batch",
            "test-batch",
            "--out",
            str(out),
            "--count",
            "2",
            "--platform",
            "fake",
            "--screen",
            "mail_list",
            "--ui-version",
            "mail-list-v2",
            "--eligible-for-current-mail-baseline",
        ]
    )
    assert exit_code == 0
    manifest_path = out / "test-batch-manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["sample_count"] == 2
    assert manifest["batch"] == "test-batch"
    assert manifest["notes"]
    for sample in manifest["samples"]:
        assert sample["file"].endswith(".png")
        assert sample["bytes"] > 0
        assert not sample["is_legacy"]
        assert sample["eligible_for_current_mail_baseline"]
        assert sample["screen"] == "mail_list"
        assert sample["ui_version"] == "mail-list-v2"
        assert len(sample["sha256"]) == 64
    assert validate_manifest(DatasetManifest.load(manifest_path), out) == []


def test_capture_cli_rejects_legacy_baseline_eligibility(tmp_path) -> None:
    try:
        capture_main(
            [
                "--batch",
                "legacy",
                "--out",
                str(tmp_path),
                "--legacy",
                "--eligible-for-current-mail-baseline",
            ]
        )
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - explicit safety assertion
        raise AssertionError("conflicting capture classifications must be rejected")
