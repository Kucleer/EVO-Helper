from __future__ import annotations

from evo_helper.domain.models import Coordinate
from evo_helper.vision.planet_scout_alert import read_planet_scout_alert


class _SecurityMail:
    def report_header(self) -> str:
        return "发件人: Aries [HQ]\n主题: 你的行星被侦察\n15/08/2026 16:34:48"

    def security_message(self) -> str:
        return (
            "一枚来自 [2:144:18] GrandSuke's Planet的敌方侦察探测器已扫描了\n"
            "你的行星 [2:137:18]。\n"
            "你的防御系统拦截了1个敌方侦察探测器中的1个。"
        )


def test_reads_source_target_and_interception_from_security_mail() -> None:
    alert = read_planet_scout_alert(_SecurityMail(), subject="你的行星被侦察")

    assert alert.source == Coordinate(2, 144, 18)
    assert alert.target == Coordinate(2, 137, 18)
    assert alert.source_name == "GrandSuke's Planet"
    assert alert.intercepted_probes == 1
    assert alert.raw_time_text == "15/08/2026 16:34:48"
