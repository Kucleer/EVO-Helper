"""Screen and UI-version classification from detector signals."""

from __future__ import annotations

from dataclasses import dataclass

from .engines import Detection
from .models import PageObservation


@dataclass(frozen=True)
class ScreenSignature:
    label: str
    screen: str
    ui_version: str
    weight: float


class PageClassifier:
    """Maps detector labels to (screen, ui_version) pairs.

    Unknown pages yield ``PageObservation`` with ``ui_version=None`` so the
    caller stops safely and preserves a diagnostic capture.
    """

    def __init__(self, signatures: tuple[ScreenSignature, ...] = ()) -> None:
        self._signatures = signatures or self._default_signatures()

    @staticmethod
    def _default_signatures() -> tuple[ScreenSignature, ...]:
        return (
            ScreenSignature("mail_list_item", "mail_list", "mail-list-v2", 1.0),
            ScreenSignature("mail_list_header", "mail_list", "mail-list-v2", 0.9),
            ScreenSignature("mail_list_pagination", "mail_list", "mail-list-v2", 0.7),
            ScreenSignature("battle_detail", "battle_detail", "battle-detail-v2", 1.0),
            ScreenSignature("battle_replay", "battle_replay", "battle-replay-v2", 1.0),
            ScreenSignature("galaxy", "galaxy", "galaxy-v2", 1.0),
            ScreenSignature("planet", "galaxy", "galaxy-v2", 0.8),
            ScreenSignature("attack_panel", "attack", "attack-v2", 1.0),
            ScreenSignature("fleet_preset", "attack", "attack-v2", 0.9),
        )

    def classify(self, detections: list[Detection]) -> PageObservation:
        best_screen: str | None = None
        best_version: str | None = None
        best_score = 0.0
        for detection in detections:
            for signature in self._signatures:
                if signature.label != detection.label:
                    continue
                score = signature.weight * detection.confidence
                if score > best_score:
                    best_score = score
                    best_screen = signature.screen
                    best_version = signature.ui_version
        if best_screen is None:
            return PageObservation(screen="unknown", ui_version=None, confidence=0.0)
        return PageObservation(screen=best_screen, ui_version=best_version, confidence=best_score)
