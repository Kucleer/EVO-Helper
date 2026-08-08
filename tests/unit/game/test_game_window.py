from __future__ import annotations

import pytest

from evo_helper.game.game_window import (
    APP_TITLE_BAR_PX,
    CALIBRATED_VIEWPORT,
    GAME_LOADING_TITLE,
    GAME_WINDOW_TITLE,
    ViewportPlan,
)


class TestViewportArithmetic:
    """页面视口 → client → window 的换算必须可逆，否则调不到标定尺寸。"""

    def test_client_adds_the_title_bar(self) -> None:
        plan = ViewportPlan()
        assert plan.client == (1920, 879 + APP_TITLE_BAR_PX)

    def test_window_adds_the_border(self) -> None:
        assert ViewportPlan().window == (1938, 926)

    def test_round_trip_from_client_back_to_viewport(self) -> None:
        plan = ViewportPlan()
        assert plan.viewport_from_client(*plan.client) == CALIBRATED_VIEWPORT

    def test_a_custom_viewport_still_round_trips(self) -> None:
        plan = ViewportPlan(viewport=(1600, 700))
        assert plan.viewport_from_client(*plan.client) == (1600, 700)

    def test_measured_client_maps_to_the_calibrated_viewport(self) -> None:
        """实测：client 1920x917 对应视口 1920x879。"""
        assert ViewportPlan().viewport_from_client(1920, 917) == (1920, 879)


class TestStrictTitleMatching:
    def test_the_game_title_is_matched_exactly(self) -> None:
        """本地控制台标题是「情报中心 · EVO-Helper」，含 EVO。

        按子串匹配会命中控制台，于是把控制台的像素喂给游戏解析器——
        而且表面上一切正常。所以必须精确匹配。
        """
        console_title = "情报中心 · EVO-Helper"
        assert GAME_WINDOW_TITLE in console_title
        assert console_title.strip() != GAME_WINDOW_TITLE

    def test_the_game_title_is_not_a_substring_check(self) -> None:
        for other in ("EVO-Helper", "EVO 账号记录", "情报中心 · EVO-Helper"):
            assert other.strip() != GAME_WINDOW_TITLE, other


class TestLoadingIsNotMissing:
    """页面重连时标题会临时退回域名。把它当成「窗口没了」会多拉一个窗口。"""

    def test_waits_for_a_loading_window_instead_of_launching_a_second_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from evo_helper.game import game_window

        # 前两次还在加载（标题是域名），第三次标题变成 EVO。
        found: list[object] = [None, None]
        launched: list[bool] = []
        monkeypatch.setattr(
            game_window,
            "find_game_window",
            lambda: found.pop(0) if found else _window("EVO"),
        )
        monkeypatch.setattr(
            game_window, "find_loading_game_window", lambda: _window(GAME_LOADING_TITLE)
        )
        monkeypatch.setattr(game_window, "launch_game", lambda: launched.append(True))
        monkeypatch.setattr(game_window, "resize_to_viewport", lambda w, p: p.viewport)

        game_window.ensure_game_window(sleep=lambda _s: None)

        # 关键断言：一个新窗口都没拉起来。
        assert launched == []

    def test_launches_only_when_nothing_is_loading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from evo_helper.game import game_window

        found: list[object] = [None]
        launched: list[bool] = []
        monkeypatch.setattr(
            game_window,
            "find_game_window",
            lambda: found.pop(0) if found else _window("EVO"),
        )
        monkeypatch.setattr(game_window, "find_loading_game_window", lambda: None)
        monkeypatch.setattr(game_window, "launch_game", lambda: launched.append(True))
        monkeypatch.setattr(game_window, "resize_to_viewport", lambda w, p: p.viewport)

        game_window.ensure_game_window(sleep=lambda _s: None)

        assert launched == [True]

    def test_a_stuck_loading_window_reports_instead_of_launching(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from evo_helper.game import game_window

        launched: list[bool] = []
        monkeypatch.setattr(game_window, "find_game_window", lambda: None)
        monkeypatch.setattr(
            game_window, "find_loading_game_window", lambda: _window(GAME_LOADING_TITLE)
        )
        monkeypatch.setattr(game_window, "launch_game", lambda: launched.append(True))
        monkeypatch.setattr(game_window, "LOAD_TIMEOUT_S", 0.0)

        with pytest.raises(game_window.GameWindowError, match="加载"):
            game_window.ensure_game_window(sleep=lambda _s: None)
        assert launched == []

    def test_duplicate_game_windows_are_refused_with_a_usable_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from evo_helper.game import game_window

        monkeypatch.setattr(
            game_window, "_windows_titled", lambda title: [_window(title), _window(title)]
        )
        with pytest.raises(game_window.GameWindowError, match="手动关掉"):
            game_window.find_game_window()


def _window(title: str) -> object:
    from evo_helper.vision.optional.window_capture import WindowInfo

    return WindowInfo(handle=1, title=title, rect=(0, 0, 1938, 926))


class TestChromeDiscovery:
    def test_missing_chrome_is_reported_not_guessed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from pathlib import Path

        from evo_helper.game import game_window

        monkeypatch.setattr(game_window, "CHROME_CANDIDATES", (Path("/nope/chrome.exe"),))
        with pytest.raises(game_window.GameWindowError, match="Chrome"):
            game_window.chrome_path()


class TestEnsureGameWindow:
    """用户随时会关掉游戏，「窗口不见了」是正常情形而不是故障。"""

    def test_an_existing_window_is_not_relaunched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from evo_helper.game import game_window

        launched: list[bool] = []
        monkeypatch.setattr(game_window, "launch_game", lambda: launched.append(True))
        monkeypatch.setattr(game_window, "find_game_window", lambda: object())
        monkeypatch.setattr(game_window, "resize_to_viewport", lambda w, p: p.viewport)

        game_window.ensure_game_window(sleep=lambda _s: None)

        assert launched == []

    def test_a_missing_window_is_relaunched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from evo_helper.game import game_window

        states = [None, None, object()]
        launched: list[bool] = []
        monkeypatch.setattr(game_window, "launch_game", lambda: launched.append(True))
        monkeypatch.setattr(game_window, "find_loading_game_window", lambda: None)
        monkeypatch.setattr(
            game_window, "find_game_window", lambda: states.pop(0) if states else object()
        )
        monkeypatch.setattr(game_window, "resize_to_viewport", lambda w, p: p.viewport)

        game_window.ensure_game_window(sleep=lambda _s: None)

        assert launched == [True]

    def test_a_window_that_never_appears_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from evo_helper.game import game_window

        monkeypatch.setattr(game_window, "launch_game", lambda: None)
        monkeypatch.setattr(game_window, "find_loading_game_window", lambda: None)
        monkeypatch.setattr(game_window, "find_game_window", lambda: None)

        with pytest.raises(game_window.GameWindowError, match="没等到窗口"):
            game_window.ensure_game_window(timeout_s=0.0, sleep=lambda _s: None)

    def test_a_wrong_viewport_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """几何不对时继续截图只会喂给解析器错位的 ROI。"""
        from evo_helper.game import game_window

        monkeypatch.setattr(game_window, "find_game_window", lambda: object())
        monkeypatch.setattr(game_window, "resize_to_viewport", lambda w, p: (1920, 992))

        with pytest.raises(game_window.GameWindowError, match="1920x992"):
            game_window.ensure_game_window(sleep=lambda _s: None)
