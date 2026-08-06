from __future__ import annotations

import json

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
        ]
    )
    assert exit_code == 0
    manifest_path = out / "test-batch-manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["sample_count"] == 2
    assert manifest["batch"] == "test-batch"
    for sample in manifest["samples"]:
        assert sample["screen"] == "mail_list"
        assert sample["ui_version"] == "mail-list-v2"
        assert len(sample["sha256"]) == 64
