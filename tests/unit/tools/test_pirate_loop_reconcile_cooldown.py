"""开工那一趟信箱**接在冷却判据上**，而不是接在一个布尔开关上。

判据本身在 `tests/unit/domain/test_reconcile_cooldown.py`。这里钉的是接线：
`run()` 每一轮都问一次冷却，该翻就真的调 `reconcile_today()`，不该翻就跳过并
把决定记下来（`_say_still_waiting` 要靠它说准话）。

⚠️ 这一整个文件的存在理由是 2026-08-15 那次改动：`run()` 里那一句被包成了
`if self._options.reconcile_on_start:`，而那个参数**没有任何生产者**——生产库
464 条 `mission_runs` 里带 `reconcile` 的是 0 条。于是「开工读战报」这条链路
在两天里一次都没跑过，而没有一条用例转红。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.reconcile_cooldown import RECONCILE_COOLDOWN
from evo_helper.tools import pirate_loop as module
from evo_helper.tools.pirate_loop import LoopOptions, Outcome, PirateLoop

ORIGIN = Coordinate(2, 137, 18)


class _FakeNavigator:
    def ensure_system_view(self, _read_labels: Any) -> bool:
        return True

    def invalidate(self) -> None:
        return None


def _loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    last_reconciled_at: datetime | None,
    force_reconcile: bool = False,
) -> tuple[Any, list[str], list[str]]:
    from evo_helper.game import game_window

    monkeypatch.setattr(game_window, "ensure_game_window", lambda: None)

    said: list[str] = []
    monkeypatch.setattr(module, "say", said.append)

    swept: list[str] = []
    loop = PirateLoop.__new__(PirateLoop)
    loop._options = LoopOptions(
        systems=(), scout=False, attack=True, origin=ORIGIN, force_reconcile=force_reconcile
    )
    loop._outcome = Outcome()
    loop._current_planet = None
    loop._navigator = _FakeNavigator()
    loop._nav_labels = lambda: ""
    loop._reset_to_known_screen = lambda: None
    loop._ensure_session = lambda **_k: False
    loop._require_system_view = lambda _what: None
    loop.ensure_origin_planet = lambda: True
    loop._reconcile_decision = None
    loop._last_reconciled_at = lambda: last_reconciled_at
    loop.reconcile_today = lambda: swept.append("翻信箱")
    loop._sweep = lambda: None
    return loop, swept, said


class TestTheRoundAsksTheCooldown:
    def test_a_link_that_never_reconciled_opens_the_mailbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ **这条就是那次故障的形状。**

        没有任何对账记录时，开工必须翻信箱。原来的行为是「默认不翻」，而
        `--reconcile` 没有任何生产者——于是战报断流两天。
        """
        loop, swept, _said = _loop(monkeypatch, last_reconciled_at=None)

        loop.run()

        assert swept == ["翻信箱"]

    def test_a_round_inside_the_cooldown_does_not_open_the_mailbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """闸门 B 的动机在这里保住了：续跑不该每一趟都翻信箱。"""
        now = datetime.now(UTC)
        loop, swept, _said = _loop(
            monkeypatch, last_reconciled_at=now - RECONCILE_COOLDOWN + timedelta(minutes=2)
        )

        loop.run()

        assert swept == []

    def test_a_round_past_the_cooldown_opens_the_mailbox(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime.now(UTC)
        loop, swept, _said = _loop(
            monkeypatch, last_reconciled_at=now - RECONCILE_COOLDOWN - timedelta(minutes=1)
        )

        loop.run()

        assert swept == ["翻信箱"]

    def test_forcing_opens_the_mailbox_even_inside_the_cooldown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--reconcile` 现在的语义：强制翻一次，忽略冷却。手工排障用。"""
        now = datetime.now(UTC)
        loop, swept, _said = _loop(
            monkeypatch, last_reconciled_at=now - timedelta(seconds=30), force_reconcile=True
        )

        loop.run()

        assert swept == ["翻信箱"]


class TestTheDecisionIsRecordedAndSaid:
    def test_the_round_says_why_it_skipped_and_when_it_last_swept(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """跳过必须留下痕迹，否则「没翻信箱」这件事在日志上根本不存在——
        而它正是那两天里唯一能看出问题的东西。
        """
        now = datetime.now(UTC)
        last = now - timedelta(minutes=2)
        loop, _swept, said = _loop(monkeypatch, last_reconciled_at=last)

        loop.run()

        assert any("本轮不翻信箱" in line for line in said)
        assert any(f"{last:%Y-%m-%d %H:%M:%S} UTC" in line for line in said)

    def test_the_decision_survives_the_round_for_the_waiting_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """决定要留在循环上：`BotLoop._say_still_waiting` 靠它区分两种措辞。"""
        now = datetime.now(UTC)
        loop, _swept, _said = _loop(monkeypatch, last_reconciled_at=now - timedelta(minutes=2))

        loop.run()

        assert loop._reconcile_decision is not None
        assert loop._reconcile_decision.sweep is False
        assert loop._reconcile_decision.last_reconciled_at_utc is not None


class TestTheLookupFailingIsNotAnExcuseToSkip:
    def test_a_broken_lookup_sweeps_rather_than_skipping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """查不到上次对账时刻时**翻**，不是跳过。

        冷却是个省钱的优化，而它省掉的那件事是这条链路的全部意义；拿不准时
        多翻一趟，比安静地不翻便宜得多——后者的代价已经付过了。
        """
        from evo_helper.game import game_window

        monkeypatch.setattr(game_window, "ensure_game_window", lambda: None)
        said: list[str] = []
        monkeypatch.setattr(module, "say", said.append)

        swept: list[str] = []
        loop = PirateLoop.__new__(PirateLoop)
        loop._options = LoopOptions(systems=(), scout=False, attack=True, origin=ORIGIN)
        loop._outcome = Outcome()
        loop._current_planet = None
        loop._navigator = _FakeNavigator()
        loop._nav_labels = lambda: ""
        loop._reset_to_known_screen = lambda: None
        loop._ensure_session = lambda **_k: False
        loop._require_system_view = lambda _what: None
        loop.ensure_origin_planet = lambda: True
        loop._reconcile_decision = None
        loop._ensure_run = lambda: (_ for _ in ()).throw(RuntimeError("连不上库"))
        loop.reconcile_today = lambda: swept.append("翻信箱")
        loop._sweep = lambda: None

        loop.run()

        assert swept == ["翻信箱"]
        assert any("查不到上次对账时刻" in line for line in said)
