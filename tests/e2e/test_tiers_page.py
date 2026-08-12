"""分档阈值页。

断言打在 `/tiers` 返回的 HTML 上：这一页的行为都在模板与它自带的那段脚本里，
取不到 HTML 就什么都守不住。接口那一侧（400 / 409 / 落库）在
`tests/integration/api/test_scheduler_api.py`。

**这里不真的 Popen 任何 runner**：`launch` 一律注入假的，后台 tick 推到一小时
一次。真起一个会去点用户的真实鼠标、派真实舰队。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evo_helper.application.mission_freeze import MissionFreezeLog
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
TOKEN = "test-token"


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _FakeLauncher:
    def __call__(self, kind: MissionKind, command: Sequence[str], log_path: Path) -> _FakeProcess:
        return _FakeProcess(pid=9001)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'console.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    supervisor = MissionSupervisor(
        launch=_FakeLauncher(), clock=lambda: NOW, log_dir=tmp_path / "logs"
    )
    app = create_persistent_app(
        factory,
        local_token=TOKEN,
        mission_scheduler=MissionScheduler(
            SqlAlchemyRepository(factory),
            supervisor,
            clock=lambda: NOW,
            # 临时目录：测试不许往仓库里落文件。
            freeze_log=MissionFreezeLog(tmp_path / "freezes.jsonl"),
        ),
        tick_interval_s=3600.0,
    )
    with TestClient(app, headers={"X-Evo-Helper-Token": TOKEN}) as test_client:
        yield test_client


def test_the_navigation_offers_the_page(client: TestClient) -> None:
    """新菜单项要在每一页的导航里，不能只有直接输网址才进得去。"""
    for path in ("/missions", "/planets", "/tiers"):
        assert '<a href="/tiers"' in client.get(path).text, path


def test_the_page_marks_itself_as_the_current_nav_item(client: TestClient) -> None:
    assert '<a href="/tiers" aria-current="page"' in client.get("/tiers").text


def test_the_page_offers_one_box_per_edge(client: TestClient) -> None:
    """三个数，一个框一个。档位数量与预设名不在这里配。"""
    html = client.get("/tiers").text

    for field in ("alpha_from", "beta_from", "gamma_from"):
        assert f'name="{field}"' in html, field
    assert "保存" in html


def test_the_boxes_are_filled_from_the_database(client: TestClient) -> None:
    """初值渲染在 HTML 里，不靠脚本补——脚本没跑起来时页面不该是三个空框。"""
    html = client.get("/tiers").text

    assert 'value="2000"' in html
    assert 'value="4000"' in html
    assert 'value="8000"' in html


def test_the_page_echoes_which_band_gets_which_preset(client: TestClient) -> None:
    """三个数看不出「哪一档打哪个区间」，那张表就是把它说出来的地方。"""
    html = client.get("/tiers").text

    assert "2K 以下" in html
    assert "2K–4K" in html
    assert "4K–8K" in html
    assert "8K+" in html
    for preset in ("AAA", "BBB", "CCC"):
        assert preset in html, preset


def test_the_page_says_the_edges_are_inclusive_lower_bounds(client: TestClient) -> None:
    """左闭右开这件事必须写出来。差一档就是派错一整套舰队。"""
    html = client.get("/tiers").text

    assert "下界" in html
    assert "严格递增" in html


def test_the_page_says_the_change_does_not_touch_history(client: TestClient) -> None:
    """用户口径（2026-08-11）：只影响启动之后要发出的攻击。

    不说清楚的话，改完阈值的人会以为历史记录里的分档结论跟着变了。
    """
    html = client.get("/tiers").text

    assert "历史" in html
    assert "现算" in html


def test_the_page_greys_the_boxes_while_the_scheduler_runs(client: TestClient) -> None:
    """后端已经 409 拒了，页面这一侧是同一条规则的**提前显形**。

    灰不灰由接口下发的 `locked` 决定，页面不自己判断调度器在不在跑——判据抄
    第二份就会出现「页面说的和调度器做的不是一回事」。
    """
    body = _page_body(client.get("/tiers").text)

    assert "state.locked" in body
    assert "/api/tier-thresholds" in body
    # 只置灰不解释，用户只会得出「这页坏了」。
    assert "运行中" in body
    assert "结束" in body


def test_the_page_shows_what_the_api_refused(client: TestClient) -> None:
    """400 / 409 都带中文说明。吞掉它，用户只会看到「点了没反应」。"""
    body = _page_body(client.get("/tiers").text)

    assert 'id="tier-error"' in body
    # `alert` 挡住页面且一次只能说一件事，这里要的是就地显示。
    assert "alert(" not in body


def test_the_page_does_not_restate_the_validation_rule_in_javascript(client: TestClient) -> None:
    """递增校验只有一份，在 `domain.fleet_tier`。

    页面上再写一遍 `a < b < c`，两把尺子迟早量出两个答案——而不一致的那一天，
    页面会放过一套后端拒绝的取值，用户看到的是「保存了又弹回去」。
    """
    body = _page_body(client.get("/tiers").text)

    assert "严格递增" in body  # 说明文字要有
    for restated in ("< beta", "< gamma", "sort(", "Math.min", "Math.max"):
        assert restated not in body, restated


def _page_body(html: str) -> str:
    """只取内容区。

    `base.html` 的公共脚本对每一页都在，拿整页做「页面上不该出现 X」这类断言，
    会被公共脚本里的 `alert(` 满足——而这一页根本没有 `form[data-api]`，
    那段代码在这里是不生效的。同 `test_missions_console._page_body`。
    """
    marker = '<div class="content">'
    start = html.find(marker)
    assert start != -1, "base.html 的内容区标记变了，这个断言得跟着改"
    return html[start:]
