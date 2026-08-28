from __future__ import annotations

from pathlib import Path

import pytest

from evo_helper.game.game_window import (
    APP_TITLE_BAR_PX,
    CALIBRATED_SCALE_FACTOR,
    CALIBRATED_VIEWPORT,
    GAME_LOADING_TITLE,
    GAME_WINDOW_TITLE,
    MAX_RESIZE_ATTEMPTS,
    ViewportPlan,
    next_window_size,
)


class TestViewportArithmetic:
    """页面视口 → client 的换算必须可逆，否则调不到标定尺寸。"""

    def test_client_adds_the_title_bar(self) -> None:
        plan = ViewportPlan()
        assert plan.client == (1920, 879 + APP_TITLE_BAR_PX)

    def test_the_plan_no_longer_guesses_a_border_width(self) -> None:
        """边框宽度跟**系统 DPI** 走，换台机器就不是本机实测的 18/9。

        原先 `ViewportPlan.window` 把 client 加上写死的 (18, 9) 一把设过去；
        在别的机器上那一把设完 client 就不是 1920x917，`ensure_game_window`
        直接抛错、整条链路起不来。现在改成量着调，这个属性不该再存在——
        留着它就会有人重新照它一把设。
        """
        assert not hasattr(ViewportPlan(), "window")

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

        monkeypatch.setattr(
            game_window, "chrome_candidates", lambda env=None: (Path("/nope/chrome.exe"),)
        )
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


def _launch_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs: object
) -> tuple[str, ...]:
    """跑一次 `launch_game`，把它交给 Chrome 的 argv 抄回来。

    **绝不真的拉起 Chrome**：`Popen` 整个换成记录器。
    """
    from evo_helper.game import game_window

    recorded: list[list[str]] = []
    monkeypatch.setattr(game_window, "chrome_path", lambda: Path("C:/fake/chrome.exe"))
    monkeypatch.setattr(
        game_window.subprocess,
        "Popen",
        lambda argv, **_kw: recorded.append(list(argv)),
    )
    game_window.launch_game(profile_dir=tmp_path / "chrome-profile", **kwargs)  # type: ignore[arg-type]
    assert len(recorded) == 1
    return tuple(recorded[0])


class TestPinnedDevicePixelRatio:
    """标定绑的不是「窗口物理尺寸」，是「物理尺寸 ÷ 缩放率」得到的 CSS 版面。

    同样一个 1920x917 的物理窗口，在 125% 缩放的机器上按 1536x703 CSS 排版，
    在 100% 的机器上按 1920x879 排版——**版面完全不同、所有坐标失效**，
    而几何校验只看物理尺寸，一路都是绿的。这是最危险的那种失效。
    """

    def test_the_page_scale_factor_is_pinned(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = _launch_argv(monkeypatch, tmp_path)
        assert f"--force-device-scale-factor={CALIBRATED_SCALE_FACTOR}" in argv

    def test_the_translate_feature_switch_is_passed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """翻译一开，整页文字被改写，认屏、关键词、OCR 会同时失效。

        实测：专用 profile 是全新的，第一次打开游戏时右上角就弹了翻译气泡。
        它本身只是遮挡（不压任何现有 ROI），但只要触发一次翻译，所有判据
        一起失效，而且看不出是翻译干的——现象是「哪儿都读不出来」。
        """
        argv = _launch_argv(monkeypatch, tmp_path)
        assert "--disable-features=Translate" in argv

    def test_the_first_run_prompts_are_suppressed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """首次运行向导和默认浏览器询问会盖在页面上，挡住要认的东西。"""
        argv = _launch_argv(monkeypatch, tmp_path)
        assert "--no-first-run" in argv
        assert "--no-default-browser-check" in argv

    def test_the_flag_really_comes_from_the_setting(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """把配置设成标定值，命令行上就该出现它。

        单看这条证明力有限（值和默认值一样），但配合下面「设成别的值就抛错」
        那条，就能证明这个开关确实是从 Settings 读的，而不是印了个字面量。
        """
        monkeypatch.setenv("EVO_HELPER_DEVICE_SCALE_FACTOR", "1.25")
        argv = _launch_argv(monkeypatch, tmp_path)
        assert "--force-device-scale-factor=1.25" in argv

    def test_a_dedicated_profile_is_used(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """没有这条，上面那条会被 Chrome **静默忽略**。

        Chrome 的命令行开关只对某个 profile 的**第一个**进程生效。用户的主
        Chrome 若已经开着，`Popen` 只是把 URL 转发给那个已有进程，
        `--force-device-scale-factor` 连看都不会看一眼——于是 DPR 还是系统
        缩放决定的那个，坐标全错，而拉起窗口这一步看着完全成功。
        """
        argv = _launch_argv(monkeypatch, tmp_path)
        flags = [item for item in argv if item.startswith("--user-data-dir=")]
        assert len(flags) == 1, argv

    def test_the_profile_path_is_absolute(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """相对路径会跟着子进程的工作目录漂——每个 cwd 一个新 profile，
        于是每次都要求重新登录，而且看起来像是「登录掉了」。"""
        argv = _launch_argv(monkeypatch, tmp_path)
        (flag,) = [item for item in argv if item.startswith("--user-data-dir=")]
        assert Path(flag.split("=", 1)[1]).is_absolute()

    def test_the_profile_directory_is_created(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        argv = _launch_argv(monkeypatch, tmp_path)
        (flag,) = [item for item in argv if item.startswith("--user-data-dir=")]
        assert Path(flag.split("=", 1)[1]).is_dir()

    def test_app_mode_is_still_used(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """普通窗口把标签栏/地址栏画进 client area，视口原点会随书签栏漂移。"""
        argv = _launch_argv(monkeypatch, tmp_path)
        assert any(item.startswith("--app=") for item in argv)

    def test_the_default_profile_lives_under_var(self) -> None:
        """`var/` 已在 .gitignore 里；profile 有几十 MB，不能进版本库。"""
        from evo_helper.game.game_window import CHROME_PROFILE_DIR

        assert CHROME_PROFILE_DIR.is_absolute()
        assert CHROME_PROFILE_DIR.parent.name == "var"


class TestScaleFactorIsCalibrationNotPreference:
    """`device_scale_factor` 不是偏好项，是标定常量。

    部署时会变的是**机器**，而钉死 DPR 的全部目的恰恰是让版面**不随机器变**。
    允许它自由取值，等于把一个「改了就全错」的标定值伪装成配置项——而它的
    错法是静默的：Chrome 的 `--app` 标题栏跟页面用同一个 scale factor，
    改成 1.0 之后标题栏从 38px 变成约 30px，`viewport_from_client` 仍按 38 减，
    **算出来还是 1920x879、几何校验照样通过**，只是每个 ROI 竖直错位 8 像素。
    """

    def test_the_calibrated_value_is_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from evo_helper.game import game_window

        monkeypatch.setenv("EVO_HELPER_DEVICE_SCALE_FACTOR", "1.25")
        assert game_window.verified_scale_factor() == CALIBRATED_SCALE_FACTOR

    def test_any_other_value_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from evo_helper.game import game_window

        monkeypatch.setenv("EVO_HELPER_DEVICE_SCALE_FACTOR", "1.0")
        with pytest.raises(game_window.GameWindowError, match="1.0"):
            game_window.verified_scale_factor()

    def test_the_error_names_what_must_be_remeasured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """光说「不许改」没用——得说清改了之后还要重新标定什么。"""
        from evo_helper.game import game_window

        monkeypatch.setenv("EVO_HELPER_DEVICE_SCALE_FACTOR", "2.0")
        with pytest.raises(game_window.GameWindowError) as caught:
            game_window.verified_scale_factor()

        message = str(caught.value)
        assert "APP_TITLE_BAR_PX" in message
        assert "CALIBRATED_VIEWPORT" in message
        assert "ROI" in message

    def test_an_explicit_argument_is_checked_too(self) -> None:
        """留个绕过校验的参数口子，等于没拦。"""
        from evo_helper.game import game_window

        with pytest.raises(game_window.GameWindowError):
            game_window.verified_scale_factor(2.0)

    def test_chrome_is_not_started_at_all_when_it_is_wrong(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """必须在拉起 Chrome **之前**拦住。

        拉起来之后再发现，屏幕上就多了一个版面不对的游戏窗口，而
        `find_game_window` 见到两个就彻底罢工，还得人工关窗口。
        """
        from evo_helper.game import game_window

        started: list[object] = []
        monkeypatch.setattr(game_window, "chrome_path", lambda: Path("C:/fake/chrome.exe"))
        monkeypatch.setattr(
            game_window.subprocess, "Popen", lambda argv, **_kw: started.append(argv)
        )
        monkeypatch.setenv("EVO_HELPER_DEVICE_SCALE_FACTOR", "1.0")

        with pytest.raises(game_window.GameWindowError):
            game_window.launch_game(profile_dir=tmp_path / "chrome-profile")
        assert started == []

    def test_an_already_open_window_is_gated_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """窗口已经开着时走不到 `launch_game`，所以那条路也要拦。

        否则「配错了 + 窗口恰好已经开着」= 拦截整个失效，而这恰恰是最常见的
        那种情形：用户先手动开了游戏，再启动助手。
        """
        from evo_helper.game import game_window

        monkeypatch.setattr(game_window, "find_game_window", lambda: object())
        monkeypatch.setattr(game_window, "resize_to_viewport", lambda w, p: p.viewport)
        monkeypatch.setenv("EVO_HELPER_DEVICE_SCALE_FACTOR", "1.0")

        with pytest.raises(game_window.GameWindowError, match="APP_TITLE_BAR_PX"):
            game_window.ensure_game_window(sleep=lambda _s: None)


class TestNextWindowSize:
    """「下一次该设多大」是纯函数，所以测得了——`MoveWindow` 测不了。"""

    def test_it_adds_the_shortfall(self) -> None:
        assert next_window_size((1920, 917), (1902, 908), (1920, 917)) == (1938, 926)

    def test_it_takes_back_the_overshoot(self) -> None:
        assert next_window_size((1920, 917), (1930, 920), (1938, 926)) == (1928, 923)

    def test_a_matching_client_leaves_the_size_alone(self) -> None:
        assert next_window_size((1920, 917), (1920, 917), (1938, 926)) == (1938, 926)


class _FakeWindowDriver:
    """假窗口：client = window - border。边框宽度由测试指定。

    真窗口既不能移动也不能测量，所以驱动整个换成假的。
    """

    def __init__(self, border: tuple[int, int]) -> None:
        self._border = border
        self._size = (0, 0)
        self.sizes: list[tuple[int, int]] = []
        self.restored = 0

    def restore(self) -> None:
        self.restored += 1

    def set_size(self, width: int, height: int) -> None:
        self._size = (width, height)
        self.sizes.append((width, height))

    def measure_client(self) -> tuple[int, int]:
        return (self._size[0] - self._border[0], self._size[1] - self._border[1])


class _FakeLifecycle:
    """假的窗口生命周期。**测试里绝不许真的开关窗口。**

    `closes` 之后再问 `find`，窗口就不见了——真窗口是异步消失的，所以这里
    还能配一个「消失前还会被看见几次」的延迟。
    """

    def __init__(self, *, present: bool = True, linger: int = 0) -> None:
        self.closes: list[int] = []
        self.ensured = 0
        self._present = present
        self._linger = linger

    def find(self) -> object | None:
        if not self._present:
            return None
        if self.closes and self._linger <= 0:
            return None
        if self.closes:
            self._linger -= 1
        return _window("EVO")

    def request_close(self, window: object) -> None:
        self.closes.append(getattr(window, "handle", 0))

    def ensure(self) -> object:
        self.ensured += 1
        return _window("EVO")


class TestRestartGameWindow:
    """「无法重新连接」那一屏的善后：关掉这一个窗口，再让它自己开回来。"""

    @pytest.fixture(autouse=True)
    def _no_real_windows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """⚠️ **本类的每一条测试都不许碰真窗口。**

        这个 fixture 不是「顺手加的保险」，是整改验证时**真的踩到了**才加的：
        把 `hardware.ensure()` 改回直接调 `ensure_game_window()`（正是这里要防的
        那种回退），下面第一条测试就沿着真实现走了下去，把用户的游戏窗口从
        1920x917 拽成了 1894x556——测试确实变红了，但**红之前已经动了系统**。

        所以光有「注入假的就不碰真的」那一条断言不够：它只保护自己那一条。
        整类一起 monkeypatch，边界一旦被绕过就立刻炸在这里，而且什么都没动。
        """
        from evo_helper.game import game_window

        def explode(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("测试绝不许碰真窗口：动系统的调用必须留在 _Win32Lifecycle 后面")

        for name in (
            "find_game_window",
            "find_loading_game_window",
            "launch_game",
            "ensure_game_window",
            "resize_to_viewport",
        ):
            monkeypatch.setattr(game_window, name, explode)

    def test_it_closes_then_brings_the_window_back(self) -> None:
        from evo_helper.game import game_window

        lifecycle = _FakeLifecycle()
        game_window.restart_game_window(lifecycle=lifecycle, pause=lambda _s: None)  # type: ignore[arg-type]

        assert lifecycle.closes == [1]
        assert lifecycle.ensured == 1

    def test_it_waits_for_the_window_to_actually_disappear(self) -> None:
        """`PostMessageW` 是异步的：投完消息窗口还在。

        不等它消失就去拉新窗口，屏幕上会同时留下两个游戏窗口，而
        `find_game_window` 见到两个就彻底罢工——链路从此起不来。
        """
        from evo_helper.game import game_window

        lifecycle = _FakeLifecycle(linger=3)
        slept: list[float] = []
        game_window.restart_game_window(lifecycle=lifecycle, pause=slept.append)  # type: ignore[arg-type]

        assert slept, "窗口还在的时候必须等，不能直接往下走"
        assert lifecycle.ensured == 1

    def test_a_window_that_refuses_to_close_is_reported_not_duplicated(self) -> None:
        from evo_helper.game import game_window

        class _Stuck(_FakeLifecycle):
            def find(self) -> object | None:
                return _window("EVO")

        lifecycle = _Stuck()
        with pytest.raises(game_window.GameWindowError, match="WM_CLOSE"):
            game_window.restart_game_window(
                lifecycle=lifecycle,  # type: ignore[arg-type]
                pause=lambda _s: None,
                clock=iter(range(0, 10_000, 5)).__next__,
            )
        assert lifecycle.ensured == 0, "关不掉就绝不能再拉一个"

    def test_a_missing_window_is_simply_relaunched(self) -> None:
        """窗口早就没了（用户自己关的、或崩了）也要能走通，不能卡在关窗上。"""
        from evo_helper.game import game_window

        lifecycle = _FakeLifecycle(present=False)
        game_window.restart_game_window(lifecycle=lifecycle, pause=lambda _s: None)  # type: ignore[arg-type]

        assert lifecycle.closes == []
        assert lifecycle.ensured == 1

    def test_it_closes_one_window_rather_than_killing_chrome(self) -> None:
        """用户多半同时开着自己的 Chrome。

        按进程名杀会把用户的标签页一起带走。`WM_CLOSE` 投到具体句柄上，
        效果等同于用户点了那个窗口的右上角 ×。
        """
        from evo_helper.game import game_window

        assert game_window.WM_CLOSE == 0x0010

    def test_every_system_touching_call_stays_behind_the_lifecycle(self) -> None:
        """**这条钉的是测试纪律，不是产品行为。**

        已经出过事：有改动在单元测试里直接调 `ensure_game_window()`，把用户真实
        的游戏窗口连拽了三次尺寸。所有会动系统的调用都必须待在 `_Win32Lifecycle`
        后面；一旦有人把它们搬回 `restart_game_window` 的函数体，`_no_real_windows`
        会让这条（和本类其余每一条）立刻变红——而且什么都没动。
        """
        from evo_helper.game import game_window

        lifecycle = _FakeLifecycle(linger=2)
        game_window.restart_game_window(lifecycle=lifecycle, pause=lambda _s: None)  # type: ignore[arg-type]

        assert (lifecycle.closes, lifecycle.ensured) == ([1], 1)

    def test_the_real_lifecycle_does_nothing_until_it_is_asked(self) -> None:
        """构造真实现本身不许有副作用，否则注入假的也拦不住它。

        （`_no_real_windows` 已经把真调用换成会炸的桩，所以这一句真去动系统就红。）
        """
        from evo_helper.game import game_window

        game_window._Win32Lifecycle()


class TestSelfCalibratingResize:
    """边框宽度跟系统 DPI 走，换台机器就变。所以不能假定，只能量。"""

    def test_it_converges_on_this_machines_border(self) -> None:
        from evo_helper.game import game_window

        driver = _FakeWindowDriver((18, 9))
        actual = game_window.resize_to_viewport(object(), driver=driver, pause=lambda _s: None)  # type: ignore[arg-type]

        assert actual == CALIBRATED_VIEWPORT
        assert driver.sizes[-1] == (1938, 926)

    def test_it_converges_on_a_border_we_did_not_assume(self) -> None:
        """这条是整改的要害：目标机器的边框不是本机的 18/9。

        只设一次就收工（改造前的行为）在这里必然落在错的 client 上。
        """
        driver = _FakeWindowDriver((26, 17))
        from evo_helper.game import game_window

        actual = game_window.resize_to_viewport(object(), driver=driver, pause=lambda _s: None)  # type: ignore[arg-type]

        assert actual == CALIBRATED_VIEWPORT
        assert driver.measure_client() == ViewportPlan().client
        assert len(driver.sizes) > 1, "只设一次不可能撞上没见过的边框"

    def test_a_zero_border_converges_on_the_first_try(self) -> None:
        """收敛了就不该再多设一次——多一次就多一次窗口跳动和 1.5s 等待。"""
        from evo_helper.game import game_window

        driver = _FakeWindowDriver((0, 0))
        game_window.resize_to_viewport(object(), driver=driver, pause=lambda _s: None)  # type: ignore[arg-type]

        assert driver.sizes == [ViewportPlan().client]

    def test_it_gives_up_instead_of_looping_forever(self) -> None:
        """窗口有最小尺寸、被贴边吸附、或者根本调不动时，必须停下来报错，
        而不是无限逼近一个永远到不了的目标。"""
        from evo_helper.game import game_window

        class _Stubborn(_FakeWindowDriver):
            def measure_client(self) -> tuple[int, int]:
                return (800, 600)

        driver = _Stubborn((0, 0))
        with pytest.raises(game_window.GameWindowError, match="800x600"):
            game_window.resize_to_viewport(object(), driver=driver, pause=lambda _s: None)  # type: ignore[arg-type]
        assert len(driver.sizes) == MAX_RESIZE_ATTEMPTS

    def test_a_maximized_window_is_restored_first(self) -> None:
        """最大化的窗口对 `SetWindowPos` 免疫——这一点踩过坑。"""
        from evo_helper.game import game_window

        driver = _FakeWindowDriver((18, 9))
        game_window.resize_to_viewport(object(), driver=driver, pause=lambda _s: None)  # type: ignore[arg-type]

        assert driver.restored == 1


class TestRestartabilityGrading:
    """哪一档「关窗重开能救」，哪一档救不了。**判据是异常类型，不是消息文本。**

    ⚠️ 这个分档是 2026-08-28 那次事故的修复面。当晚游戏窗口没了，六个任务每一轮
    起来约 1 秒就 `exit=1`，而会重开 Chrome 的那套机制在 `BotLoop.run()` 内部、
    够不着上面那一行的 `driver.window()`——整夜没有任何东西尝试重开过窗口。

    分档的判据写在 `GameWindowMissing` 上，一句话：**同样的一步再走一遍，结论会不会
    不一样**。重开本身就是「关掉窗口 → `ensure_game_window()` 重走一遍」，所以失败
    发生在那一步之前（配置、Chrome 位置、DPR、桌面上有几个同名窗口）或者发生在
    窗口已经拿到之后的几何校验上时，再走一遍必然是同一个结论。

    钉在**抛出的那一处**而不是异常类上：类本身怎么继承是它自己的事，真正会改错的
    是某个 `raise` 挑错了类型，而那只有从这一头才看得见。
    """

    def test_a_window_that_never_appears_is_worth_a_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """⚠️ **这一句就是昨夜那一档**：Chrome 挂了 / 机器休眠，窗口拉不起来。"""
        from evo_helper.game import game_window

        monkeypatch.setattr(game_window, "launch_game", lambda: None)
        monkeypatch.setattr(game_window, "find_loading_game_window", lambda: None)
        monkeypatch.setattr(game_window, "find_game_window", lambda: None)

        with pytest.raises(game_window.GameWindowMissing):
            game_window.ensure_game_window(timeout_s=0.0, sleep=lambda _s: None)

    def test_a_wrong_viewport_is_not_worth_a_restart(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """几何调不到标定值是**标定**问题：新窗口在同一台机器上照样调不到。"""
        from evo_helper.game import game_window

        monkeypatch.setattr(game_window, "find_game_window", lambda: object())
        monkeypatch.setattr(game_window, "resize_to_viewport", lambda w, p: (1920, 992))

        with pytest.raises(game_window.GameWindowError) as failure:
            game_window.ensure_game_window(sleep=lambda _s: None)
        assert not isinstance(failure.value, game_window.GameWindowMissing)

    def test_a_resize_that_never_converges_is_not_worth_a_restart(self) -> None:
        """同上，只是失败发生在更里面一层。"""
        from evo_helper.game import game_window

        class _Stubborn(_FakeWindowDriver):
            def measure_client(self) -> tuple[int, int]:
                return (800, 600)

        with pytest.raises(game_window.GameWindowError) as failure:
            game_window.resize_to_viewport(  # type: ignore[arg-type]
                object(), driver=_Stubborn((0, 0)), pause=lambda _s: None
            )
        assert not isinstance(failure.value, game_window.GameWindowMissing)

    def test_a_misconfigured_dpr_is_not_worth_a_restart(self) -> None:
        """配置问题。`ensure_game_window` 第一句就再校验一次，重开必然倒在同一处。"""
        from evo_helper.game import game_window

        with pytest.raises(game_window.GameWindowError) as failure:
            game_window.verified_scale_factor(1.0)
        assert not isinstance(failure.value, game_window.GameWindowMissing)

    def test_two_windows_with_the_same_title_are_not_worth_a_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`restart_game_window` 第一句 `find()` 就抛同一个错。

        而且这一档的消息本来就写着「请手动关掉多余的那个」——它要的是人，不是重试。
        """
        from evo_helper.game import game_window

        monkeypatch.setattr(game_window, "_windows_titled", lambda _t: [object(), object()])

        with pytest.raises(game_window.GameWindowError) as failure:
            game_window.find_game_window()
        assert not isinstance(failure.value, game_window.GameWindowMissing)

    def test_a_window_stuck_on_the_loading_title_is_not_worth_a_restart(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`find_game_window()` 按**精确**标题匹配，那个窗口的标题还是域名。

        所以重开根本关不掉它（`WM_CLOSE` 送不到），只会再等一遍同样的 180 秒，
        然后留下同一个卡住的窗口。
        """
        from evo_helper.game import game_window

        monkeypatch.setattr(game_window, "find_game_window", lambda: None)
        monkeypatch.setattr(game_window, "find_loading_game_window", lambda: object())
        monkeypatch.setattr(game_window, "_wait_for_game_window", lambda *_a, **_k: None)

        with pytest.raises(game_window.GameWindowError) as failure:
            game_window.ensure_game_window(sleep=lambda _s: None)
        assert not isinstance(failure.value, game_window.GameWindowMissing)

    def test_the_foreground_race_stays_its_own_grade(self) -> None:
        """「抢不到前台」和「窗口不见了」是兄弟，不是父子。

        混成一档的代价是把「什么都不做、纯粹让路」那一档变成「关掉窗口再抢前台」
        ——用户正在用别的窗口的时候，那比什么都不做糟得多。
        """
        from evo_helper.game import game_window

        assert not issubclass(game_window.ForegroundUnavailable, game_window.GameWindowMissing)
        assert not issubclass(game_window.GameWindowMissing, game_window.ForegroundUnavailable)
