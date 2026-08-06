"""Explicit, auditable run state transitions."""

from .models import RunState

_ACTIVE = {RunState.ARMED, RunState.SCANNING, RunState.WAITING_CAPACITY, RunState.DRAINING}
_NORMAL_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.DRAFT: {RunState.ARMED},
    RunState.ARMED: {RunState.SCANNING},
    RunState.SCANNING: {RunState.WAITING_CAPACITY, RunState.DRAINING},
    RunState.WAITING_CAPACITY: {RunState.SCANNING},
    RunState.DRAINING: {RunState.COMPLETED},
}


def can_transition(current: RunState, target: RunState) -> bool:
    """Return whether a service may record the requested state transition."""
    if current in _ACTIVE and target in {
        RunState.PAUSED,
        RunState.FAILED,
        RunState.EMERGENCY_STOPPED,
    }:
        return True
    if current is RunState.PAUSED and target is RunState.ARMED:
        return True
    return target in _NORMAL_TRANSITIONS.get(current, set())


def require_transition(current: RunState, target: RunState) -> None:
    if not can_transition(current, target):
        raise ValueError(f"invalid run state transition: {current} -> {target}")
