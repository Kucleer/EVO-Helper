"""Dataset manifest validation and annotation helpers."""

from __future__ import annotations

import argparse
from pathlib import Path

from evo_helper.datasets.manifest import (
    DatasetManifest,
    validate_capture_manifest,
    validate_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate a manifest against its files")
    validate.add_argument("manifest", type=Path)
    validate.add_argument("--base-dir", type=Path, default=Path("."))
    validate.add_argument(
        "--capture-evidence",
        action="store_true",
        help="require complete browser-capture evidence metadata",
    )

    annotate = sub.add_parser("annotate", help="mark 7/21 regression eligibility")
    annotate.add_argument("manifest", type=Path)
    annotate.add_argument(
        "--eligible", nargs="*", default=[], help="sample files eligible for battle regression"
    )
    args = parser.parse_args(argv)

    if args.command == "validate":
        manifest = DatasetManifest.load(args.manifest)
        validator = validate_capture_manifest if args.capture_evidence else validate_manifest
        errors = validator(manifest, args.base_dir)
        for error in errors:
            print(f"ERROR: {error}")
        print(f"{'FAIL' if errors else 'OK'} ({len(manifest.samples)} samples)")
        return 1 if errors else 0

    if args.command == "annotate":
        manifest = DatasetManifest.load(args.manifest)
        eligible = set(args.eligible)
        for entry in manifest.samples:
            print(
                f"{entry.file}: legacy={entry.is_legacy} "
                f"eligible={entry.eligible_for_current_mail_baseline}"
            )
        print(f"regression-eligible set: {sorted(eligible)}")
        return 0
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
