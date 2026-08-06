"""Vision pipeline: detectors -> OCR -> fusion -> typed observations."""

from __future__ import annotations

from .classifier import PageClassifier
from .engines import DetectorEngine, OcrEngine, TemplateMatcher
from .fusion import CoordinateFusion, NameFusion
from .models import (
    BattleDetail,
    BattleReplay,
    GalaxyObservation,
    MailListObservation,
    PageObservation,
    PresetSignatureCheck,
)
from .parsers import (
    check_preset_signature,
    parse_battle_detail,
    parse_battle_replay,
    parse_galaxy,
    parse_mail_list,
)


class VisionPipeline:
    """Coordinates detectors, OCR, and template matching for one screen."""

    def __init__(
        self,
        detector: DetectorEngine,
        ocr: OcrEngine,
        matcher: TemplateMatcher,
        classifier: PageClassifier | None = None,
        coordinate_fusion: CoordinateFusion | None = None,
        name_fusion: NameFusion | None = None,
    ) -> None:
        self._detector = detector
        self._ocr = ocr
        self._matcher = matcher
        self._classifier = classifier or PageClassifier()
        self._coordinate_fusion = coordinate_fusion or CoordinateFusion()
        self._name_fusion = name_fusion or NameFusion()

    def observe_page(self, image: object) -> PageObservation:
        detections = self._detector.detect(image)
        return self._classifier.classify(detections)

    def mail_list(self, image: object, frames: int = 1) -> MailListObservation:
        page = self._observe_consistent_page(image, frames)
        return parse_mail_list(page, self._ocr.recognize(image), "ocr")

    def battle_detail(self, image: object, frames: int = 1) -> BattleDetail:
        page = self._observe_consistent_page(image, frames)
        return parse_battle_detail(page, self._ocr.recognize(image), "ocr")

    def battle_replay(self, image: object, frames: int = 1) -> BattleReplay:
        page = self._observe_consistent_page(image, frames)
        return parse_battle_replay(page, self._ocr.recognize(image), "ocr")

    def galaxy(self, image: object, frames: int = 1) -> GalaxyObservation:
        page = self._observe_consistent_page(image, frames)
        return parse_galaxy(page, self._ocr.recognize(image), "ocr")

    def preset_signature(
        self,
        image: object,
        expected_name: str,
        expected_signature: str,
        frames: int = 1,
    ) -> PresetSignatureCheck:
        page = self._observe_consistent_page(image, frames)
        return check_preset_signature(
            page, expected_name, expected_signature, self._ocr.recognize(image), "ocr"
        )

    def _observe_consistent_page(self, image: object, frames: int) -> PageObservation:
        """Confirm the same page/version across consecutive frames."""
        pages = [self.observe_page(image) for _ in range(max(1, frames))]
        first = pages[0]
        if any(
            page.ui_version != first.ui_version or page.screen != first.screen for page in pages[1:]
        ):
            return PageObservation(screen=first.screen, ui_version=None, confidence=0.0)
        return first
