"""Fixed-viewport screenshot capture with manifest and SHA-256 evidence."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from evo_helper.datasets.manifest import compute_sha256


@dataclass(frozen=True)
class CapturedImage:
    """A capture record that is directly consumable by ``DatasetManifest``."""

    file: str
    bytes: int
    is_legacy: bool
    eligible_for_current_mail_baseline: bool
    artifact_id: str
    captured_at_utc: str
    session_id: str
    batch: str
    screen: str
    ui_version: str
    viewport: dict[str, object]
    sha256: str
    source: str


class CapturePlatform(Protocol):
    def grab(self, path: Path) -> None: ...


class MssCapturePlatform:
    """Optional real screen capture via mss."""

    def __init__(self, monitor: int = 1) -> None:
        self._monitor = monitor

    def grab(self, path: Path) -> None:
        try:
            import mss
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError("mss is not installed; use --platform fake for tests") from exc
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[self._monitor])
            from mss.tools import to_png

            to_png(shot.rgb, shot.size, output=str(path))


class FakeCapturePlatform:
    def __init__(self, content: bytes = b"fake-png") -> None:
        self._content = content
        self.captured: list[Path] = []

    def grab(self, path: Path) -> None:
        path.write_bytes(self._content)
        self.captured.append(path)


def build_manifest(entries: list[CapturedImage]) -> dict[str, object]:
    return {
        "batch": entries[0].batch if entries else "empty",
        "captured_at_utc": datetime.now(UTC).isoformat(),
        "sample_count": len(entries),
        "notes": "Captured by evo_helper.tools.capture; baseline eligibility is explicit.",
        "samples": [asdict(entry) for entry in entries],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", required=True, help="manifest batch name")
    parser.add_argument("--out", default="var/captures", help="output directory")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--platform", choices=["fake", "mss"], default="fake")
    parser.add_argument("--screen", default="mail_list")
    parser.add_argument("--ui-version", default="mail-list-v2")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--source", default="evo-capture")
    parser.add_argument(
        "--legacy",
        action="store_true",
        help="mark captures as archival-only and ineligible for the current mail baseline",
    )
    parser.add_argument(
        "--eligible-for-current-mail-baseline",
        action="store_true",
        help="explicitly allow non-legacy mail-list captures into the current baseline",
    )
    args = parser.parse_args(argv)
    if args.legacy and args.eligible_for_current_mail_baseline:
        parser.error("legacy captures cannot be eligible for the current mail baseline")
    if args.eligible_for_current_mail_baseline and args.screen != "mail_list":
        parser.error("only mail_list captures can enter the current mail baseline")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    platform = FakeCapturePlatform() if args.platform == "fake" else MssCapturePlatform()
    session_id = args.session_id or str(uuid4())
    entries: list[CapturedImage] = []
    for index in range(args.count):
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        filename = f"{args.batch}-{timestamp}-{index:03d}.png"
        path = out / filename
        platform.grab(path)
        entries.append(
            CapturedImage(
                file=filename,
                bytes=path.stat().st_size,
                is_legacy=args.legacy,
                eligible_for_current_mail_baseline=args.eligible_for_current_mail_baseline,
                artifact_id=str(uuid4()),
                captured_at_utc=datetime.now(UTC).isoformat(),
                session_id=session_id,
                batch=args.batch,
                screen=args.screen,
                ui_version=args.ui_version,
                viewport={"width": 1920, "height": 1080, "scale": 1.0},
                sha256=compute_sha256(path),
                source=args.source,
            )
        )
    manifest_path = out / f"{args.batch}-manifest.json"
    manifest_path.write_text(
        json.dumps(build_manifest(entries), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"captured {len(entries)} sample(s); manifest: {manifest_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
