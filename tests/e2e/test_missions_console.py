"""调度台页面。

断言全部打在 `/missions` 返回的 HTML 上：这一页的行为几乎都在模板与它自带的
那段脚本里，取不到 HTML 就什么都守不住。

**这里不真的 Popen 任何 runner**：`launch` 一律注入假的，后台 tick 推到一小时
一次。真起一个会去点用户的真实鼠标、派真实舰队。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.scheduler import MissionKind, TaskStatus
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
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
            SqlAlchemyRepository(factory), supervisor, clock=lambda: NOW
        ),
        # 后台 tick 先 sleep 再 tick，推到一小时就等于「测试期间不会自己跑」。
        tick_interval_s=3600.0,
    )
    with TestClient(app, headers={"X-Evo-Helper-Token": TOKEN}) as test_client:
        yield test_client


def test_the_page_lists_the_three_tasks(client: TestClient) -> None:
    html = client.get("/missions").text

    assert "侦查+攻击海盗" in html
    assert "扫描+攻击 bot" in html
    assert "扫描全星系 bot" in html


def test_the_old_plan_form_is_gone(client: TestClient) -> None:
    """那个表单产出的计划行没有任何 runner 会读。

    填了没人读的表单，比没有表单更害人。
    """
    html = client.get("/missions").text

    assert "新建扫描任务" not in html
    assert "扫描区段" not in html
    assert "/api/plans" not in html


def test_the_time_window_chip_is_gone(client: TestClient) -> None:
    """定时没了，这个 chip 就是句谎话。"""
    html = client.get("/missions").text

    assert "时间窗口 UTC+8" not in html


def test_the_page_offers_start_and_stop(client: TestClient) -> None:
    html = client.get("/missions").text

    assert "/api/scheduler/start" in html
    assert "/api/scheduler/stop" in html


def test_the_scan_row_is_not_draggable_and_says_why(client: TestClient) -> None:
    """扫描恒在最后一位，页面上就不能给它一个能拖的把手。

    它永远有活干，排在谁前面谁就永远轮不到——拖到海盗之前等于当天 32 次
    配额悄无声息地全流失。后端会拒（`PATCH /api/missions/SCAN` 带 priority
    返回 400），但用户不该拖完了才发现。
    """
    html = client.get("/missions").text
    scan_row = _row_html(html, "SCAN")

    assert 'draggable="true"' not in scan_row
    assert 'draggable="false"' in scan_row
    assert "始终填空隙" in scan_row

    # 另外两行必须真的能拖，否则「扫描不可拖」这条断言用一个全都不能拖的
    # 页面也能满足。
    for kind in ("PIRATE", "BOT"):
        assert 'draggable="true"' in _row_html(html, kind), kind


def test_all_eight_statuses_survive_the_trip_to_the_page(client: TestClient) -> None:
    """八档一个都不能合并。

    没勾的任务显示「待命」是谎话（它永远不会被起起来）；冷却中显示「等航线」
    会让用户去调航线数、调完还是不动。页面按状态上色，所以每一档都得在色调表
    里各占一格——少一格就意味着有两档被当成了同一件事。
    """
    html = client.get("/missions").text

    for status in TaskStatus:
        assert status.value in html, status.name


def test_the_bot_row_carries_a_new_round_button(client: TestClient) -> None:
    """bot 打完一轮就退出调度，**不自动开下一轮**——开新一轮只能是用户按的。"""
    bot_row = _row_html(client.get("/missions").text, "BOT")

    assert "重开一轮" in bot_row
    assert "/api/missions/BOT/new-round" in bot_row


def test_the_page_offers_force_kill_for_an_orphan(client: TestClient) -> None:
    """孤儿红条：上次没走正常关闭路径留下的进程号。"""
    html = client.get("/missions").text

    assert "/api/scheduler/force-kill" in html
    assert "强制结束" in html


def test_the_status_area_polls_instead_of_reloading_the_page(client: TestClient) -> None:
    """刷整页会清掉用户正在输入的参数框。"""
    body = _page_body(client.get("/missions").text)

    assert "/api/scheduler" in body
    assert "setInterval" in body
    assert "location.reload" not in body


def test_the_page_shows_what_the_api_refused(client: TestClient) -> None:
    """参数不合格时后端返回 400 带中文说明，静默失败等于把它扔了。"""
    body = _page_body(client.get("/missions").text)

    assert 'id="mission-error"' in body
    # `alert` 挡住页面且一次只能说一件事，这里要的是就地显示。
    assert "alert(" not in body


def test_the_page_lists_the_mission_run_history(client: TestClient) -> None:
    html = client.get("/missions").text

    assert "运行历史" in html
    assert "结束方式" in html
    assert "退出码" in html


def test_the_page_does_not_recompute_the_scheduling_criteria(client: TestClient) -> None:
    """状态文案一律用后端下发的 status / detail / summary。

    页面自己算一遍「该不该跑」，就会出现「页面说的和调度器做的不是一回事」——
    那种错静默，且只有在舰队白飞一趟之后才看得见。
    """
    html = client.get("/missions").text

    for criterion in ("pirate_daily_quota", "restart_cooldown", "has_work", "空闲航线"):
        assert criterion not in html, criterion


def _page_body(html: str) -> str:
    """只取这一页自己那段，不含 `base.html` 的骨架。

    骨架里那个通用表单处理器带着 `alert(` 和 `location.reload`，整页搜会被它
    满足——而这一页根本没有 `form[data-api]`，那段代码在这里是不生效的。
    """
    marker = '<div class="content">'
    start = html.find(marker)
    assert start != -1, "base.html 的内容区标记变了，这个断言得跟着改"
    return html[start:]


def _row_html(html: str, kind: str) -> str:
    """截出某一条链路那一行的 HTML。

    整页搜 `draggable="false"` 会被别的行满足，所以断言必须落在具体的行上。
    """
    marker = f'data-kind="{kind}"'
    start = html.find(marker)
    assert start != -1, f"页面上没有 {kind} 这一行"
    start = html.rfind("<tr", 0, start)
    end = html.find("</tr>", start)
    assert end != -1
    return html[start:end]
