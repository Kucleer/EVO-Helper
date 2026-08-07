"""Explicit, auditable run state transitions."""

from .models import RunState

# 等待中的运行同样必须能暂停和紧急停止——否则舰队在飞的几个小时里无法干预。
_ACTIVE = {
    RunState.ARMED,
    RunState.SCANNING,
    RunState.WAITING_CAPACITY,
    RunState.DRAINING,
    RunState.AWAITING_REPORT,
    RunState.WAITING_SESSION,
}
_NORMAL_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.DRAFT: {RunState.ARMED},
    RunState.ARMED: {RunState.SCANNING},
    RunState.SCANNING: {RunState.WAITING_CAPACITY, RunState.DRAINING},
    RunState.WAITING_CAPACITY: {RunState.SCANNING},
    # 排空时如果战报都还在飞，就松手去等；到点再回到 DRAINING 收取。
    RunState.DRAINING: {RunState.COMPLETED, RunState.AWAITING_REPORT},
    RunState.AWAITING_REPORT: {RunState.DRAINING, RunState.WAITING_SESSION},
    # 拿不到登录时在这两个状态之间来回：退避、再试。
    RunState.WAITING_SESSION: {RunState.DRAINING, RunState.AWAITING_REPORT},
}


def can_transition(current: RunState, target: RunState) -> bool:
    """Return whether a service may record the requested state transition."""
    if current in _ACTIVE and target in {
        RunState.PAUSED,
        RunState.FAILED,
        RunState.EMERGENCY_STOPPED,
    }:
        return True
    if current is RunState.PAUSED and target in {RunState.ARMED, RunState.AWAITING_REPORT}:
        # 一个在等战报时被暂停的运行，恢复后应该接着等战报，而不是从头重新扫描。
        return True
    return target in _NORMAL_TRANSITIONS.get(current, set())


def require_transition(current: RunState, target: RunState) -> None:
    if not can_transition(current, target):
        raise ValueError(f"invalid run state transition: {current} -> {target}")
