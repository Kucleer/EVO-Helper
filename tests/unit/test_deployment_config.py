"""部署相关的三处硬编码必须真的走配置。

这三处（Tesseract 路径、Chrome 路径、主星坐标）的共同点是：**换台机器或换个
账号就错，而且错得静默**。Tesseract 找不到会在第一次 OCR 时炸在很深的地方；
主星填错则一声不响地让舰队从别人的星球出发。所以这里测的不是「Settings 上有
这个字段」，而是「真正用它的那几个地方确实跟着环境变量走」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from evo_helper.config import Settings
from evo_helper.domain.missions import ORIGIN
from evo_helper.domain.models import Coordinate


class TestTesseractPath:
    def test_the_default_is_the_usual_windows_install(self) -> None:
        assert Settings().tesseract_path == r"C:\Program Files\Tesseract-OCR\tesseract.exe"

    def test_it_follows_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVO_HELPER_TESSERACT_PATH", r"D:\ocr\tesseract.exe")
        assert Settings().tesseract_path == r"D:\ocr\tesseract.exe"

    def test_the_scan_runner_resolves_it_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """必须是函数而不是模块常量。

        模块常量在 import 那一刻就把值定死了，之后再读 `.env` 或环境变量都
        不生效——而「配置改了却没生效」不报错，只是 OCR 仍旧去找那个不存在
        的路径，报错点离原因十万八千里。
        """
        from evo_helper.tools import scan_coordinates

        monkeypatch.setenv("EVO_HELPER_TESSERACT_PATH", r"D:\ocr\tesseract.exe")
        assert scan_coordinates.tesseract_path() == Path(r"D:\ocr\tesseract.exe")

    def test_the_two_runners_borrow_the_same_resolver(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """三条链路各写一遍 = 改一处漏两处，而漏掉的那两条照常启动。

        bot 那条**不再自己解析**：取 `ReportScreens` 的代码合并进了
        `PirateLoop._report_screens`，路径只在那一处取。这里顺带钉住「没有第二份」
        ——重新长出一个 `bot_loop._tesseract()` 就是把这个坑重新挖开。
        """
        from evo_helper.tools import bot_loop, pirate_loop, scan_coordinates

        monkeypatch.setenv("EVO_HELPER_TESSERACT_PATH", r"D:\ocr\tesseract.exe")

        assert pirate_loop._tesseract_path() == Path(r"D:\ocr\tesseract.exe")
        assert scan_coordinates.tesseract_path() == Path(r"D:\ocr\tesseract.exe")
        assert not hasattr(bot_loop, "_tesseract")

    @pytest.mark.parametrize(
        "module_name",
        ["ingest_report", "ingest_pirate_report", "repair_ship_names"],
    )
    def test_the_ingest_tools_default_to_the_configured_path(
        self, monkeypatch: pytest.MonkeyPatch, module_name: str
    ) -> None:
        import importlib

        monkeypatch.setenv("EVO_HELPER_TESSERACT_PATH", r"D:\ocr\tesseract.exe")
        module = importlib.import_module(f"evo_helper.tools.{module_name}")
        parser = module.build_parser()
        # 只给必填项，`--tesseract` 留空走默认值。
        required = {
            "ingest_report": ["--detail", "a.png", "--replay", "b.png"],
            "ingest_pirate_report": ["--detail", "a.png", "--bottom", "b.png"],
            "repair_ship_names": ["--replay", "a.png", "--report", "x"],
        }[module_name]
        assert parser.parse_args(required).tesseract == r"D:\ocr\tesseract.exe"


class TestChromePath:
    def test_the_user_level_install_is_the_first_candidate(self) -> None:
        """Chrome 默认装在 `%LOCALAPPDATA%`，不是 `Program Files`。

        只列系统级路径时，一台按默认方式装 Chrome 的机器会直接报「找不到
        Chrome」，而机器上明明装着。

        env 是**注入的**，不读本机的——CI 在 Linux 上跑，那里根本没有
        `LOCALAPPDATA`。读本机的话这条测试在 CI 上必挂（实际挂过一次）。
        """
        from evo_helper.game.game_window import chrome_candidates

        local = r"C:\Users\someone\AppData\Local"
        candidates = chrome_candidates({"LOCALAPPDATA": local})
        expected = Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"
        assert candidates[0] == expected, candidates

    def test_a_missing_localappdata_drops_the_candidate_rather_than_going_relative(self) -> None:
        """`LOCALAPPDATA` 缺失时整条略过，不许拼出相对路径。

        `Path("") / "Google" / ...` 是**相对路径**，会去匹配当前工作目录下的
        同名文件——找 Chrome 找到工作目录里去，是那种查起来要命的错。
        """
        from pathlib import PureWindowsPath

        from evo_helper.game.game_window import SYSTEM_CHROME_CANDIDATES, chrome_candidates

        assert chrome_candidates({}) == SYSTEM_CHROME_CANDIDATES
        # 用 `PureWindowsPath` 而不是 `Path.is_absolute()`：在 Linux（CI 就在那儿跑）
        # 上 `Path(r"C:\...")` 是 PosixPath，没有前导 `/`，`is_absolute()` 恒为 False。
        # 这条断言的第一版就是这么挂的——和它要防的那个 bug 是同一类平台假设。
        assert all(PureWindowsPath(str(c)).is_absolute() for c in chrome_candidates({}))

    def test_an_explicit_path_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        from evo_helper.game import game_window

        fake = tmp_path / "chrome.exe"
        fake.write_bytes(b"")
        monkeypatch.setenv("EVO_HELPER_CHROME_PATH", str(fake))
        assert game_window.chrome_path() == fake

    def test_an_explicit_path_that_is_wrong_is_reported_not_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """配了却找不到时**不能**悄悄回落到候选列表。

        回落的话，用户以为在跑自己指定的那个 Chrome（比如带着已登录的
        profile），实际跑的是另一个——差别要等到登录失败才看得出来。
        """
        from evo_helper.game import game_window

        missing = tmp_path / "nope.exe"
        monkeypatch.setenv("EVO_HELPER_CHROME_PATH", str(missing))
        with pytest.raises(game_window.GameWindowError, match="nope.exe"):
            game_window.chrome_path()


class TestOrigin:
    def test_the_default_still_matches_the_domain_constant(self) -> None:
        """默认值必须和 `domain.missions.ORIGIN` 一致。

        领域层保留默认值是刻意的：`domain` 不许 import `config`，否则纯领域
        层就绑死在配置上。所以两边只能靠这条测试对齐。
        """
        assert Settings().origin_coordinate == ORIGIN

    def test_it_follows_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVO_HELPER_ORIGIN", "3:42:7")
        assert Settings().origin_coordinate == Coordinate(3, 42, 7)

    def test_a_malformed_origin_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """填错格式必须当场报错。

        默默回落到默认主星 = 舰队从上一个账号的星球出发，飞行时间、战报匹配
        全跟着错，而且完全不报错。
        """
        monkeypatch.setenv("EVO_HELPER_ORIGIN", "2:137")
        with pytest.raises(ValueError, match="2:137"):
            Settings().origin_coordinate

    def test_a_non_positive_component_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("EVO_HELPER_ORIGIN", "2:137:0")
        with pytest.raises(ValueError):
            Settings().origin_coordinate

    def test_the_scan_runner_resolves_it_at_call_time(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from evo_helper.tools import scan_coordinates

        monkeypatch.setenv("EVO_HELPER_ORIGIN", "3:42:7")
        assert scan_coordinates.origin() == Coordinate(3, 42, 7)

    def test_the_pirate_runner_borrows_the_same_resolver(self) -> None:
        from evo_helper.tools import pirate_loop, scan_coordinates

        assert pirate_loop.origin is scan_coordinates.origin

    def test_the_scheduler_builds_commands_from_the_injected_origin(self) -> None:
        """调度器算出来的 `--systems` 是围绕主星展开的。

        这是「主星配错」唯一能在派遣之前被看见的地方。
        """
        from evo_helper.application.mission_scheduler import MissionScheduler
        from evo_helper.domain.scheduler import MissionKind

        scheduler = MissionScheduler(cast(Any, None), cast(Any, None), origin=Coordinate(3, 42, 7))
        command = scheduler.command_for(
            MissionKind.PIRATE, '{"radius": 1}', origin=Coordinate(3, 42, 7)
        )

        assert "3:42" in command
        assert not any(item.startswith("2:") for item in command)

    def test_the_console_summary_follows_the_scheduler(self) -> None:
        """页面回显的范围和调度器真正会打的范围必须是同一个主星。

        两边各读一次默认值的话，配了 `EVO_HELPER_ORIGIN` 之后页面会显示
        旧主星、舰队却从新主星出发——用户看着「没问题」，打的却是别处。
        """
        from evo_helper.application.mission_scheduler import MissionScheduler
        from evo_helper.web.persistent_service import MissionConsoleService

        scheduler = MissionScheduler(cast(Any, None), cast(Any, None), origin=Coordinate(3, 42, 7))
        console = MissionConsoleService(cast(Any, None), scheduler)

        assert "3:41" in console._pirate_summary(scheduler.origin, {"radius": 1})
