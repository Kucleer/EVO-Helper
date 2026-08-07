"""Humanised mouse input for read-only capture navigation.

Every click is offset, timed and paced randomly. A run of pixel-exact clicks on
a fixed cadence is the clearest automation signature a game can look for, so
this module never emits one.

This adapter is for navigation during evidence capture. It refuses any label
that reads like a dispatch, claim or delete: the final attack click is the
ActionGuard's job and must not be reachable from a capture tool.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any, Protocol

#: Clicks land within this many pixels of the requested point.
CLICK_JITTER_PX = 4

#: Seconds to wait after an action, sampled uniformly from this band.
MIN_CLICK_DELAY_S = 0.35
MAX_CLICK_DELAY_S = 1.10

#: Cursor travel time, sampled uniformly from this band.
MIN_MOVE_S = 0.12
MAX_MOVE_S = 0.45

#: Substrings that must never be clicked by a capture run. Matched
#: case-insensitively against the caller's label.
FORBIDDEN_LABELS = (
    "派遣",
    "攻击",
    "删除",
    "领取",
    "取消",
    "dispatch",
    "attack",
    "delete",
    "claim",
    "send",
)


class NavigationOnlyError(RuntimeError):
    """Raised when a capture run tries to click something that acts on the game."""


class PointerBackend(Protocol):
    FAILSAFE: bool

    def moveTo(self, x: int, y: int, duration: float = ...) -> None: ...  # noqa: N802
    def click(self) -> None: ...
    def dragTo(self, x: int, y: int, duration: float = ...) -> None: ...  # noqa: N802


def load_pyautogui() -> Any:
    try:
        import pyautogui
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("pyautogui is required for capture navigation") from exc
    pyautogui.FAILSAFE = True
    return pyautogui


class HumanInput:
    """Drives the pointer the way a person would, and only for navigation."""

    def __init__(
        self,
        backend: PointerBackend,
        *,
        seed: int | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not backend.FAILSAFE:
            raise RuntimeError(
                "pyautogui.FAILSAFE is disabled; the emergency stop would not work. "
                "Refusing to drive the pointer."
            )
        self._backend = backend
        self._random = random.Random(seed)
        self._sleep = sleep

    def click(self, x: int, y: int, *, label: str = "") -> None:
        _reject_acting_label(label)
        target_x, target_y = self._jitter(x, y)
        self._backend.moveTo(target_x, target_y, self._move_duration())
        self._backend.click()
        self._pause()

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str = "") -> None:
        """Drag inside a panel. Game panels ignore the wheel and scroll only by drag."""
        _reject_acting_label(label)
        start_x, start_y = self._jitter(from_x, from_y)
        end_x, end_y = self._jitter(to_x, to_y)
        self._backend.moveTo(start_x, start_y, self._move_duration())
        self._backend.dragTo(end_x, end_y, self._move_duration())
        self._pause()

    def _jitter(self, x: int, y: int) -> tuple[int, int]:
        return (
            x + self._random.randint(-CLICK_JITTER_PX, CLICK_JITTER_PX),
            y + self._random.randint(-CLICK_JITTER_PX, CLICK_JITTER_PX),
        )

    def _move_duration(self) -> float:
        return self._random.uniform(MIN_MOVE_S, MAX_MOVE_S)

    def _pause(self) -> None:
        self._sleep(self._random.uniform(MIN_CLICK_DELAY_S, MAX_CLICK_DELAY_S))


def _reject_acting_label(label: str) -> None:
    lowered = label.lower()
    for forbidden in FORBIDDEN_LABELS:
        if forbidden.lower() in lowered:
            raise NavigationOnlyError(
                f"refusing to click {label!r}: a capture run is read-only, and "
                f"{forbidden!r} acts on the game"
            )
