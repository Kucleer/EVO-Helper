"""ActionGuard: the only path that may authorize a final dispatch click.

Safety invariants (docs/safety.md, frozen contracts):
- A dispatch requires a fresh, consistent re-observation immediately before the
  click; stale or uncertain observations are refused.
- Tokens are single-use, short-lived, and bound to one dispatch command.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from evo_helper.domain.models import DispatchCommand
from evo_helper.domain.ports import ScreenObservation


@dataclass(frozen=True)
class ActionGuardToken:
    value: UUID
    command: DispatchCommand
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ActionGuardDecision:
    allowed: bool
    reason: str
    token: ActionGuardToken | None = None


class ActionGuard:
    """Issues and consumes single-use dispatch tokens after safety checks."""

    def __init__(
        self,
        *,
        ttl: timedelta = timedelta(seconds=30),
        min_page_confidence: float = 0.99,
        required_page: str = "attack",
    ) -> None:
        self._ttl = ttl
        self._min_page_confidence = min_page_confidence
        self._required_page = required_page
        self._issued: dict[UUID, ActionGuardToken] = {}
        self._consumed: set[UUID] = set()

    def evaluate(
        self, command: DispatchCommand, observation: ScreenObservation
    ) -> ActionGuardDecision:
        """Run the pre-dispatch gate; on success a token is issued (not yet used)."""
        if observation.ui_version is None:
            return ActionGuardDecision(False, "UI version unknown: refusing dispatch")
        if observation.screen != self._required_page:
            return ActionGuardDecision(
                False, f"expected {self._required_page!r}, observed {observation.screen!r}"
            )
        if observation.confidence < self._min_page_confidence:
            return ActionGuardDecision(
                False,
                "page confidence "
                f"{observation.confidence:.3f} below {self._min_page_confidence:.3f}",
            )
        token = ActionGuardToken(
            value=uuid4(),
            command=command,
            issued_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + self._ttl,
        )
        self._issued[token.value] = token
        return ActionGuardDecision(True, "guard cleared; token issued", token)

    def verify_and_consume(
        self, token: ActionGuardToken, observation: ScreenObservation
    ) -> ActionGuardDecision:
        """Final check immediately before a click: token must be valid and unused."""
        if token.value not in self._issued:
            return ActionGuardDecision(False, "unknown token")
        if token.value in self._consumed:
            return ActionGuardDecision(False, "token already consumed")
        if datetime.now(UTC) > token.expires_at:
            return ActionGuardDecision(False, "token expired")
        if observation.ui_version is None or observation.confidence < self._min_page_confidence:
            return ActionGuardDecision(False, "re-observation did not confirm a consistent screen")
        if observation.screen != self._required_page:
            return ActionGuardDecision(
                False,
                f"expected {self._required_page!r} immediately before dispatch, "
                f"observed {observation.screen!r}",
            )
        self._consumed.add(token.value)
        return ActionGuardDecision(True, "token consumed; dispatch authorized")
