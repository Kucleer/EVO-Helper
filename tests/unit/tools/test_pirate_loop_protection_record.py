"""撞上保护期时 runner 做的两件事：**落库**，以及**说清楚排除到什么时候**。

判据不是「有没有打日志」，而是**出事时能不能只靠库里的日志定位**。所以这一条
钉的是内容而不是条数：哪个坐标、排除到什么时候、依据是什么，一样都不能少。

⚠️ **类别按弹窗类型认，不按那句中文认**（`pirate_ui.DialogKind`）。这几句中文是
从屏幕上 OCR 出来再贴回词表的，字面本来就会抖（实机把「派遣」读成过「派遗」），
而「跳过这个目标」和「停下整轮」做反的代价极不对称。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import DEFAULT_PROTECTION_EXCLUSION
from evo_helper.game import pirate_ui
from evo_helper.tools.pirate_loop import Outcome, PirateLoop

TARGET = Coordinate(4, 393, 10)


class _Driver:
    def click(self, _x: int, _y: int, *, label: str = "") -> None:
        pass

    def wait(self, _seconds: float) -> None:
        pass


class _Config:
    def __init__(self, hours: int | None) -> None:
        self.protection_exclusion_hours = hours


class _Repository:
    """只记下「谁在什么时候被记了保护期」。"""

    def __init__(self, *, hours: int | None = None, has_row: bool = True) -> None:
        self.noted: list[tuple[Coordinate, datetime]] = []
        self._hours = hours
        self._has_row = has_row

    def note_protection_period(self, coordinate: Coordinate, *, seen_at_utc: datetime) -> bool:
        self.noted.append((coordinate, seen_at_utc))
        return self._has_row

    def military_attack_config(self) -> _Config:
        return _Config(self._hours)


def _loop(repository: _Repository) -> Any:
    loop = PirateLoop.__new__(PirateLoop)
    loop._driver = _Driver()  # type: ignore[attr-defined]
    loop._outcome = Outcome()  # type: ignore[attr-defined]
    loop._read = lambda *_a, **_k: pirate_ui.DIALOG_NO_MISSION  # type: ignore[attr-defined, assignment]
    loop._ensure_run = lambda: (repository, None)  # type: ignore[attr-defined, assignment]
    return loop


@pytest.fixture(autouse=True)
def _quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    from evo_helper.tools import pirate_loop as module

    monkeypatch.setattr(module, "say", lambda _m: None)


def _logs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """接住 `record_system_log`，只留 (level, message, payload)。"""
    from evo_helper.tools import pirate_loop as module

    captured: list[tuple[str, str, dict[str, Any]]] = []

    def _record(level, source, message, *, payload=None, **_kw):  # type: ignore[no-untyped-def]
        captured.append((level, message, dict(payload or {})))

    monkeypatch.setattr(module, "record_system_log", _record)
    return captured


# -- 落库 ----------------------------------------------------------------------


def test_hitting_the_dialog_records_the_protection_period(monkeypatch: pytest.MonkeyPatch) -> None:
    """**弹窗一出现就写库。** 没有这一步，下一轮选靶查不到，同样的目标会被原样
    重挑——实机 2026-08-18 那一轮 11.5 分钟一发没派，一秒后的下一轮照挑不误。
    """
    _logs(monkeypatch)
    repository = _Repository()
    loop = _loop(repository)

    assert loop._handle_dialog(TARGET) is False, "保护期只跳过这一个目标，不停整轮"

    assert [coordinate for coordinate, _seen in repository.noted] == [TARGET]
    seen_at = repository.noted[0][1]
    assert seen_at.tzinfo is not None, "存的必须是 aware 的 UTC，判据全建立在这上面"


@pytest.mark.parametrize("message", [pirate_ui.DIALOG_NO_SHIPS, pirate_ui.DIALOG_LINES_FULL])
def test_the_other_two_dialogs_never_touch_the_protection_column(
    monkeypatch: pytest.MonkeyPatch, message: str
) -> None:
    """资源耗尽那两个弹窗**和保护期毫无关系**，一个字都不许往那一列写。

    写了的话，一次「航线占满」会把当时那个目标锁在候选池外 8 小时——而它其实
    随时能打，只是刚才没航线。
    """
    from evo_helper.tools.pirate_loop import RoundExhausted

    _logs(monkeypatch)
    repository = _Repository()
    loop = _loop(repository)
    loop._read = lambda *_a, **_k: message  # type: ignore[assignment]

    with pytest.raises(RoundExhausted):
        loop._handle_dialog(TARGET)

    assert repository.noted == []


# -- 日志 ----------------------------------------------------------------------


def test_the_log_line_says_which_target_until_when_and_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一条日志要能独立回答三个问题：**哪个坐标、排除到什么时候、依据是什么。**

    少任何一个，读日志的人都得回来读代码才知道这一轮为什么少了一个目标——
    而「出事时能只靠库里的日志定位」正是这条功能的验收判据。
    """
    captured = _logs(monkeypatch)
    repository = _Repository()
    loop = _loop(repository)

    loop._handle_dialog(TARGET)

    assert len(captured) == 1, "每个目标每次撞上写一条，不限流也不重复"
    level, message, payload = captured[0]
    assert level == "INFO"
    assert str(TARGET) in message
    assert pirate_ui.DIALOG_NO_MISSION in message, "依据是那个弹窗，日志上要看得见"
    seen_at = datetime.fromisoformat(payload["seen_at_utc"])
    until = datetime.fromisoformat(payload["excluded_until_utc"])
    assert until - seen_at == DEFAULT_PROTECTION_EXCLUSION
    assert payload["exclusion_hours"] == 8
    assert payload["target"] == str(TARGET)
    assert payload["recorded"] is True


def test_a_configured_window_shows_up_in_the_log_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """旋钮改过之后，日志里那句「排除到什么时候」要跟着改。

    不跟的话，排障的人照着代码里的 8 小时去推，怎么算都对不上——一个被改过的
    阈值最阴的失败方式正是这个。
    """
    captured = _logs(monkeypatch)
    loop = _loop(_Repository(hours=2))

    loop._handle_dialog(TARGET)

    _level, _message, payload = captured[0]
    seen_at = datetime.fromisoformat(payload["seen_at_utc"])
    until = datetime.fromisoformat(payload["excluded_until_utc"])
    assert until - seen_at == timedelta(hours=2)
    assert payload["exclusion_hours"] == 2


def test_a_failed_record_is_reported_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """`bot_targets` 里没有这一行 → **明说没记上**，级别升到 WARNING。

    默不作声的话日志看起来像是记上了，而下一轮照样会挑中它——
    「日志说假话比不说更糟」，这个仓为此拖过两天的故障。
    """
    captured = _logs(monkeypatch)
    loop = _loop(_Repository(has_row=False))

    loop._handle_dialog(TARGET)

    level, message, payload = captured[0]
    assert level == "WARNING"
    assert payload["recorded"] is False
    assert "排除不掉" in message


def test_an_unreadable_knob_falls_back_to_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """读不到配置 → 走代码默认值，**不把整轮弄死**。

    一个查不出来的配置说明不了「用户想改它」；而这一侧只拿这个数写日志，
    真正的排除在选靶那边现读同一列，所以退回默认最多让预告偏一点。
    """
    captured = _logs(monkeypatch)
    repository = _Repository()
    repository.military_attack_config = _boom  # type: ignore[method-assign]
    loop = _loop(repository)

    loop._handle_dialog(TARGET)

    _level, _message, payload = captured[0]
    assert payload["exclusion_hours"] == 8
    assert repository.noted, "配置读不出来不该连落库一起放弃"


def _boom() -> _Config:
    raise RuntimeError("库连不上")


# -- 判据认的是类型，不是那句中文 ----------------------------------------------


def test_the_branch_is_keyed_on_the_dialog_kind() -> None:
    """三个弹窗，两类。词表是封闭的，这张分类表也必须是封闭的。

    ⚠️ 新增弹窗却不给它定类别时 `dialog_kind` 直接 `KeyError`——**那正是想要的**。
    「认不出就当没弹窗放行」的默认行为会让 runner 对着一个它没看懂的屏继续点。
    """
    assert pirate_ui.dialog_kind(pirate_ui.DIALOG_NO_MISSION) is pirate_ui.DialogKind.PROTECTED
    assert pirate_ui.dialog_kind(pirate_ui.DIALOG_NO_SHIPS) is pirate_ui.DialogKind.EXHAUSTED
    assert pirate_ui.dialog_kind(pirate_ui.DIALOG_LINES_FULL) is pirate_ui.DialogKind.EXHAUSTED
    assert set(pirate_ui.DIALOG_MESSAGES) == {
        pirate_ui.DIALOG_NO_MISSION,
        pirate_ui.DIALOG_NO_SHIPS,
        pirate_ui.DIALOG_LINES_FULL,
    }
    with pytest.raises(KeyError):
        pirate_ui.dialog_kind("服务器维护中")


def test_a_misread_dialog_still_records_the_protection_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OCR 少认一个字照样要记上。

    实机把「派遣」读成过「派遗」。判据若是那句中文的字面比较，差一个字就漏——
    而漏掉的后果是这个目标下一轮又被挑中，症状和这次修的缺陷一模一样。
    `snap_dialog` 先把文案贴回词表，之后一律按 `DialogKind` 分流。
    """
    _logs(monkeypatch)
    repository = _Repository()
    loop = _loop(repository)
    loop._read = lambda *_a, **_k: "没有可执行的仼务。"  # type: ignore[assignment]

    assert loop._handle_dialog(TARGET) is False
    assert [coordinate for coordinate, _seen in repository.noted] == [TARGET]


def test_the_current_time_is_read_once_per_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """落库的时刻和日志里那个时刻**必须是同一个**。

    各取一次 `now()` 的话两者会差几微秒——单看无害，但它意味着「排除到什么时候」
    这句预告算的不是库里那一行的起点，而排障时正是拿这两个数对着看的。
    """
    captured = _logs(monkeypatch)
    repository = _Repository()
    loop = _loop(repository)

    before = datetime.now(UTC)
    loop._handle_dialog(TARGET)
    after = datetime.now(UTC)

    _level, _message, payload = captured[0]
    stored = repository.noted[0][1]
    assert stored == datetime.fromisoformat(payload["seen_at_utc"])
    assert before <= stored <= after
