import pytest

from evo_helper.config import Settings


def test_defaults_are_dry_run_and_loopback_only() -> None:
    settings = Settings()

    assert settings.dry_run is True
    assert settings.host == "127.0.0.1"


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
