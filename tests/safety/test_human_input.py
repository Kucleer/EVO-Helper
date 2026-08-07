from __future__ import annotations

import pytest

from evo_helper.game.human_input import (
    CLICK_JITTER_PX,
    MAX_CLICK_DELAY_S,
    MIN_CLICK_DELAY_S,
    HumanInput,
    NavigationOnlyError,
)


class FakePyAutoGui:
    """Records what a real pyautogui would have been asked to do."""

    FAILSAFE = True

    def __init__(self) -> None:
        self.moves: list[tuple[int, int, float]] = []
        self.clicks: list[tuple[int, int]] = []
        self.drags: list[tuple[int, int, float]] = []
        self.sleeps: list[float] = []

    def moveTo(self, x: int, y: int, duration: float = 0.0) -> None:  # noqa: N802
        self.moves.append((x, y, duration))

    def click(self) -> None:
        self.clicks.append(self.moves[-1][:2])

    def mouseDown(self) -> None:  # noqa: N802
        pass

    def mouseUp(self) -> None:  # noqa: N802
        pass

    def dragTo(self, x: int, y: int, duration: float = 0.0) -> None:  # noqa: N802
        self.drags.append((x, y, duration))


@pytest.fixture
def gui() -> FakePyAutoGui:
    return FakePyAutoGui()


def make(gui: FakePyAutoGui, seed: int = 1) -> HumanInput:
    return HumanInput(gui, seed=seed, sleep=gui.sleeps.append)


class TestFailsafe:
    def test_failsafe_must_be_enabled_on_construction(self, gui: FakePyAutoGui) -> None:
        """The emergency stop is flinging the mouse into a corner; it must work."""
        gui.FAILSAFE = False
        with pytest.raises(RuntimeError, match="FAILSAFE"):
            make(gui)

    def test_construction_does_not_turn_failsafe_off(self, gui: FakePyAutoGui) -> None:
        make(gui)
        assert gui.FAILSAFE is True


class TestHumanisedClicks:
    def test_click_lands_within_the_jitter_radius(self, gui: FakePyAutoGui) -> None:
        make(gui).click(800, 400)

        (x, y) = gui.clicks[0]
        assert abs(x - 800) <= CLICK_JITTER_PX
        assert abs(y - 400) <= CLICK_JITTER_PX

    def test_click_is_offset_rather_than_pixel_exact(self, gui: FakePyAutoGui) -> None:
        """A run of pixel-exact clicks is the clearest automation signature."""
        human = make(gui)
        for _ in range(12):
            human.click(800, 400)

        assert len({point for point in gui.clicks}) > 1

    def test_move_duration_varies(self, gui: FakePyAutoGui) -> None:
        human = make(gui)
        for _ in range(12):
            human.click(800, 400)

        assert len({round(duration, 4) for _, _, duration in gui.moves}) > 1

    def test_delay_between_clicks_is_within_the_configured_band(self, gui: FakePyAutoGui) -> None:
        human = make(gui)
        for _ in range(20):
            human.click(800, 400)

        assert gui.sleeps
        for delay in gui.sleeps:
            assert MIN_CLICK_DELAY_S <= delay <= MAX_CLICK_DELAY_S

    def test_delay_is_not_a_fixed_cadence(self, gui: FakePyAutoGui) -> None:
        human = make(gui)
        for _ in range(20):
            human.click(800, 400)

        assert len({round(delay, 4) for delay in gui.sleeps}) > 1

    def test_same_seed_replays_identically(self, gui: FakePyAutoGui) -> None:
        other = FakePyAutoGui()
        make(gui, seed=7).click(100, 200)
        make(other, seed=7).click(100, 200)

        assert gui.clicks == other.clicks


class TestDragToScroll:
    def test_drag_is_humanised_too(self, gui: FakePyAutoGui) -> None:
        make(gui).drag(784, 520, 784, 220)

        assert gui.drags
        (x, y, duration) = gui.drags[0]
        assert abs(x - 784) <= CLICK_JITTER_PX
        assert abs(y - 220) <= CLICK_JITTER_PX
        assert duration > 0


class TestReadOnlyGuard:
    def test_refuses_a_label_that_dispatches(self, gui: FakePyAutoGui) -> None:
        """Capture navigation must never reach the dispatch button."""
        with pytest.raises(NavigationOnlyError, match="派遣"):
            make(gui).click(800, 400, label="派遣")

    def test_refuses_delete_and_claim_labels(self, gui: FakePyAutoGui) -> None:
        for label in ("删除", "领取", "attack", "dispatch"):
            with pytest.raises(NavigationOnlyError):
                make(gui).click(800, 400, label=label)

    def test_blocked_label_performs_no_input(self, gui: FakePyAutoGui) -> None:
        with pytest.raises(NavigationOnlyError):
            make(gui).click(800, 400, label="dispatch")

        assert gui.clicks == []
        assert gui.moves == []

    def test_navigation_labels_are_allowed(self, gui: FakePyAutoGui) -> None:
        make(gui).click(800, 400, label="邮件")
        assert len(gui.clicks) == 1
