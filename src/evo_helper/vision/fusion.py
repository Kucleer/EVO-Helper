"""Multi-frame, multi-source fusion with strict consistency rules."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TypeVar

from evo_helper.domain.models import Coordinate

from .models import CoordinateParse, NameParse

T = TypeVar("T")


@dataclass(frozen=True)
class ConsistencyResult:
    consistent: bool
    value: str
    confidence: float
    sources: tuple[str, ...]
    conflicting_sources: tuple[str, ...] = ()


def _best(values: list[tuple[str, str, float]], required_sources: int) -> ConsistencyResult | None:
    """Fuse (source, value, confidence) votes.

    A value is accepted only when at least ``required_sources`` distinct sources
    agree and the fused confidence clears the safety threshold. Conflicting
    votes are reported so callers can refuse to act.
    """
    votes_by_value: dict[str, dict[str, float]] = defaultdict(dict)
    for source, value, confidence in values:
        votes_by_value[value][source] = max(votes_by_value[value].get(source, 0.0), confidence)

    candidates: list[tuple[str, float, list[str]]] = []
    for value, source_confidences in votes_by_value.items():
        if len(source_confidences) >= required_sources:
            confidence = sum(source_confidences.values()) / len(source_confidences)
            candidates.append((value, confidence, sorted(source_confidences)))
    if not candidates:
        return None
    value, confidence, sources = max(candidates, key=lambda item: (item[1], len(item[2])))
    conflicts = tuple(
        sorted(
            set(
                source
                for source_confidences in votes_by_value.values()
                for source in source_confidences
            )
            - set(sources)
        )
    )
    return ConsistencyResult(
        consistent=True,
        value=value,
        confidence=confidence,
        sources=tuple(sources),
        conflicting_sources=conflicts,
    )


class CoordinateFusion:
    """Require three independent sources to agree before a coordinate is trusted."""

    def __init__(self, required_sources: int = 3, min_confidence: float = 0.995) -> None:
        if required_sources < 1:
            raise ValueError("required_sources must be positive")
        self.required_sources = required_sources
        self.min_confidence = min_confidence

    def fuse(self, votes: list[tuple[str, str, float]]) -> CoordinateParse | None:
        result = _best(votes, self.required_sources)
        if result is None or result.conflicting_sources or result.confidence < self.min_confidence:
            return None
        galaxy, system, position = (int(part) for part in result.value.split(":"))
        return CoordinateParse(
            value=Coordinate(galaxy, system, position),
            confidence=result.confidence,
            sources=result.sources,
        )


class NameFusion:
    def __init__(self, required_sources: int = 2, min_confidence: float = 0.99) -> None:
        self.required_sources = required_sources
        self.min_confidence = min_confidence

    def fuse(self, votes: list[tuple[str, str, float]]) -> NameParse | None:
        result = _best(votes, self.required_sources)
        if result is None or result.conflicting_sources or result.confidence < self.min_confidence:
            return None
        return NameParse(value=result.value, confidence=result.confidence, sources=result.sources)
