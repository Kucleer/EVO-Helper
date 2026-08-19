import pytest

from evo_helper.config import Settings


def test_the_console_listens_on_the_lan_by_default() -> None:
    """局域网可访问是明确需求；端口刻意避开 8000。

    ⚠️ 判据落在**字段默认值**上，不是 `Settings()`：`.env.example` 里写的是
    `EVO_HELPER_HOST=127.0.0.1`、`EVO_HELPER_PORT=8000`，所以照它建过 `.env` 的
    开发机上 `Settings()` 读到的是被覆盖后的值，这条在本地必红；而 CI 没有 `.env`，
    永远绿。同 `database_url` 那条，方向相反、同一个病。

    `lan_exposed` 这条仍要走一次实例：判据是「默认那个 host 会被判成已暴露」，
    而不只是「默认值等于某个字符串」。host 用显式关键字传进去，`.env` 盖不着它。
    """
    host = Settings.model_fields["host"].default
    port = Settings.model_fields["port"].default

    assert host == "0.0.0.0"  # noqa: S104 - 见 Settings.host 的说明
    assert port == 8770
    assert Settings(host=host).lan_exposed is True


def test_a_missing_env_file_does_not_fall_back_to_an_empty_sqlite_file() -> None:
    """⚠️ 读不到配置时必须**响地失败**，不许静默换一个空库。

    `env_file=".env"` 按当前工作目录解析，所以从别的目录起控制台就读不到它。
    旧默认值是 `sqlite:///var/evo-helper.db`：那会在当时的目录下新建一个空库，
    控制台照常启动、页面照常打开、一个错都不报——而所有数据都不见了。生产已
    全面切到 PostgreSQL，这条把「静默回落」这个失败方式钉死在外面。

    ⚠️ 判据落在**字段默认值**上，不是 `Settings().database_url`：开发机上有 `.env`，
    那个值会被 `.env` 满足，于是这条在本地永远绿——绿得毫无意义。
    """
    default = Settings.model_fields["database_url"].default

    assert isinstance(default, str)
    assert not default.startswith("sqlite")


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
