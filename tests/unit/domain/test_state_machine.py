import pytest

from evo_helper.domain.models import RunState
from evo_helper.domain.state_machine import can_transition, require_transition


def test_normal_and_safety_transitions_are_explicit() -> None:
    assert can_transition(RunState.DRAFT, RunState.ARMED)
    assert can_transition(RunState.SCANNING, RunState.DRAINING)
    assert can_transition(RunState.WAITING_CAPACITY, RunState.EMERGENCY_STOPPED)
    assert not can_transition(RunState.DRAFT, RunState.SCANNING)
    assert not can_transition(RunState.COMPLETED, RunState.SCANNING)


def test_invalid_transition_raises() -> None:
    with pytest.raises(ValueError, match="invalid"):
        require_transition(RunState.DRAFT, RunState.COMPLETED)
