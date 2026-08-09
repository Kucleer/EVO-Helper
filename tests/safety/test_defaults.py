import pytest

from evo_helper.config import Settings


def test_dry_run_stays_the_default() -> None:
    """绑定地址已按需求放开到局域网，但**演习模式仍然是默认**。

    这两件事从前写在同一个断言里，放开监听时很容易顺手把 dry_run 一起改掉。
    拆开是为了让「默认不真的点游戏」单独有人守着。
    """
    assert Settings().dry_run is True


def test_the_console_listens_on_the_lan_by_default() -> None:
    """局域网可访问是明确需求；端口刻意避开 8000。"""
    settings = Settings()

    assert settings.host == "0.0.0.0"  # noqa: S104 - 见 Settings.host 的说明
    assert settings.port == 8770
    assert settings.lan_exposed is True


def test_loopback_binding_is_not_reported_as_exposed() -> None:
    """设回 127.0.0.1 时不该再打「已暴露」的警告，否则警告会被无视。"""
    assert Settings(host="127.0.0.1").lan_exposed is False


class TestDispatchBriefingGuard:
    """派攻击之前必须确认简报上写的是「攻击」。

    类型选错会把舰队派成探索或运输——拿不到战报，还白烧一趟燃料。
    简报页是点「出发！」之前的最后一屏，也正是该做这个确认的地方。
    """

    def briefing(self, mission: str) -> object:
        from evo_helper.vision.parsers import parse_dispatch_briefing

        return parse_dispatch_briefing(
            "任务类型:    " + mission + "\n"
            "飞行时间:    28分 21秒\n"
            "预计到达时间（约）:    07/08/2026 10:28:28\n"
        )

    def test_only_an_attack_briefing_may_be_dispatched_as_an_attack(self) -> None:
        assert self.briefing("攻击").is_attack
        for other in ("探索", "运输", "回收", "侦察", "某种新任务"):
            assert not self.briefing(other).is_attack, other

    def test_the_expected_report_time_comes_from_the_absolute_arrival(self) -> None:
        """用绝对到达时间，而不是「当前时间 + 时长」：不依赖时钟同步，也不会漂移。"""
        from datetime import UTC, datetime

        briefing = self.briefing("攻击")
        assert briefing.expected_report_at_utc == datetime(2026, 8, 7, 10, 28, 28, tzinfo=UTC)

    def test_a_briefing_missing_its_arrival_time_cannot_be_dispatched(self) -> None:
        from evo_helper.vision.parsers import UnknownUiVersionError, parse_dispatch_briefing

        with pytest.raises(UnknownUiVersionError):
            parse_dispatch_briefing("任务类型:    攻击\n飞行时间:    28分 21秒\n")
