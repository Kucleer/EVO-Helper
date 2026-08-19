"""调度台上的「战报补录」那一段。

断言全部打在 `/missions` 返回的 HTML 上：这一段的行为都在模板与它自带的那段脚本
里，取不到 HTML 就什么都守不住。接口那一侧在 `tests/integration/api/test_backfill_api.py`。

**这里不真的 Popen 任何东西**：任务与补录两侧的 `launch` 都注入假的。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evo_helper.application.backfill import BackfillCoordinator, BackfillPhase, default_since
from evo_helper.application.mission_freeze import MissionFreezeLog
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app
from support.database import scratch_database_url

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
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


class _FakeBackfillLauncher:
    def __call__(self, command: Sequence[str], log_path: Path) -> _FakeProcess:
        return _FakeProcess(pid=8001)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    engine = create_database_engine(scratch_database_url(tmp_path, "console.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    app = create_persistent_app(
        factory,
        local_token=TOKEN,
        mission_scheduler=MissionScheduler(
            SqlAlchemyRepository(factory),
            MissionSupervisor(launch=_FakeLauncher(), clock=lambda: NOW, log_dir=tmp_path / "logs"),
            clock=lambda: NOW,
            freeze_log=MissionFreezeLog(tmp_path / "freezes.jsonl"),
            backfill=BackfillCoordinator(
                launch=_FakeBackfillLauncher(), clock=lambda: NOW, log_dir=tmp_path / "logs"
            ),
        ),
        tick_interval_s=3600.0,
    )
    with TestClient(app, headers={"X-Evo-Helper-Token": TOKEN}) as test_client:
        yield test_client


def _page_body(html: str) -> str:
    """页面自带的那段脚本。断言打在它上面，不是打在整页 HTML 上。"""
    return html[html.index("<script>") :]


def test_the_page_has_a_manual_backfill_control(client: TestClient) -> None:
    html = client.get("/missions").text

    assert "战报补录" in html
    assert 'id="btn-backfill"' in html
    assert 'id="backfill-kind"' in html
    assert 'id="backfill-since"' in html


def test_both_chains_are_offered(client: TestClient) -> None:
    """两条链路的信箱主题不同，一趟只读得了一种——少一个选项就等于那条链路
    在页面上补不了。
    """
    html = client.get("/missions").text

    assert 'value="pirate"' in html
    assert 'value="bot"' in html


def test_the_default_start_date_is_yesterday_in_utc(client: TestClient) -> None:
    """**默认「今天」会把用户要补的那批整批藏起来。**

    游戏时间按 UTC+0 显示，UTC 的今天要到现实时间早上 8 点才开始；昨夜漏掉的
    战报会被 `--since 今天` 全部排除在外，而漏掉的恰恰就是昨夜的。
    情报中心那个「舰队总数 > 0」的默认筛选踩过同一个坑。

    默认值由**服务端**算：浏览器算的是本地时区（UTC+8），早上 8 点之前会差一天。
    """
    html = client.get("/missions").text

    expected = default_since(datetime.now(UTC)).isoformat()
    assert f'id="backfill-since" value="{expected}"' in html
    assert expected != datetime.now(UTC).date().isoformat()


def test_the_start_button_does_not_reconcile_by_default(client: TestClient) -> None:
    """「先对账再跑任务」**默认不勾**（2026-08-13 实机之后改的）。

    那一趟失败时闸门要等人点确认才放行任务，而无人值守时没人点——凌晨崩一次，
    之后一整夜一个任务都不起（理由整段在 `web.schemas.SchedulerStartIn.reconcile`）。

    ⚠️ **页面这一处必须跟另外两个默认值一起改。** 实机 2026-08-13 只改了 schema
    默认，用户点「开始」照样先对账——因为页面不走 schema 默认，它把复选框的值
    **显式**送出去，而那个框当时默认勾着。这条用例就是钉这一点的。
    """
    html = client.get("/missions").text

    assert 'id="start-reconcile"' in html, "复选框本身还在（人在跟前时仍然可以勾）"
    assert 'id="start-reconcile" checked' not in html


def test_the_page_says_why_the_tasks_are_paused(client: TestClient) -> None:
    """补录扣着窗口时，任务那几行会一直显示「待命」而什么都不起。不写原因的话，
    那看起来就是调度器坏了——而它其实正在按顺序办事。
    """
    body = _page_body(client.get("/missions").text)

    assert "任务已暂停" in body
    assert "补录优先于任务" in body


def test_the_page_shows_the_log_tail(client: TestClient) -> None:
    """补录最坏要跑十几分钟，按钮点下去之后页面不能只是「没反应」。"""
    html = client.get("/missions").text

    assert 'id="backfill-log"' in html
    assert "log_tail" in _page_body(html)


def test_the_page_shows_the_three_summary_numbers(client: TestClient) -> None:
    """跑完要把「改了什么」摆出来：补进来几份、认领上几发、几个 bot 目标
    不用再打了。第三个就是省下来的重复攻击。
    """
    body = _page_body(client.get("/missions").text)

    for field in ("reports_ingested", "dispatches_claimed", "bot_targets_settled"):
        assert field in body, field
    # 「量了 0 个」和「一个都没变」不是一回事。
    assert "bot_targets_measured" in body


def test_the_resume_button_is_tied_to_the_awaiting_flag(client: TestClient) -> None:
    """放行只能由用户点。显隐跟着接口下发的 `awaiting_ack` 走，页面不自己判断
    补录跑完没有——抄一份判据就会有一天页面说「可以放行了」而闸门还关着。
    """
    body = _page_body(client.get("/missions").text)

    lines = [line for line in body.splitlines() if "btn-backfill-resume" in line]
    assert lines, "页面上没有「继续任务」按钮了"
    assert any("awaiting_ack" in line for line in lines)


def test_every_phase_has_a_glyph_next_to_its_colour(client: TestClient) -> None:
    """色永远配一个字形和一个词：控制台要在灰度下、对色盲用户也读得懂。"""
    html = client.get("/missions").text

    for phase in BackfillPhase:
        assert phase.value in html, phase.name
    body = _page_body(html)
    assert "BACKFILL_GLYPHS" in body
    assert "BACKFILL_TONES" in body


def test_the_page_still_renders_without_a_console(tmp_path: Path) -> None:
    """假服务那条路上没有调度器，`/api/backfill` 压根不存在。

    照样渲染出那一段的话，页面上会有一块点了就报错的面板；而整页崩掉更糟。
    """
    from evo_helper.web.app import create_app

    app = create_app()

    with TestClient(app) as client:
        html = client.get("/missions").text

    # 认那一段自己的 id，不认「战报补录」四个字：脚本里那句分节注释一直都在
    # （它归 `BACKFILL_ENABLED` 那个开关管），认字面量会把注释也算成面板。
    assert 'id="backfill-head"' not in html
    assert 'id="btn-backfill"' not in html
    assert "调度器" in html


# -- 展示层报错 ≠ 业务中断 -------------------------------------------------------
#
# 2026-08-19 的事故：面板紧凑化（7924bd4）删掉了 `backfill-log-path` 那个节点，
# 却把脚本里那句无条件的 `getElementById('backfill-log-path').textContent = ...`
# 留在原地。用户点下「开始补录」的那一刻状态里就有了 `log_path`，于是**此后每一次
# 渲染**都在 `null` 上写 `textContent`，面板上一片红：
#
#     TypeError: Cannot set properties of null (setting 'textContent')
#
# 补录本身完全不看 DOM，所以那条异常一发都没少补；但用户看到的是一块红着的面板，
# 于是把一趟正在正常翻信箱的补录取消掉了。


def _missions_script(html: str) -> str:
    """**这一页自己**那段脚本。

    `_page_body` 从第一个 `<script>` 起算，里面还裹着 `base.html` 那段共用脚本；
    共用脚本服务的是所有页面，它去拿的节点本来就不该在这一页上都有（比如
    「每 15 秒自动刷新」那个开关，攻击日志与系统日志两页才挂）。拿它一起判，
    这条用例第一天就是红的。
    """
    return html[html.rindex("<script>") :]


def _referenced_ids(script: str) -> set[str]:
    """脚本要去页面上拿的每一个 id。

    三种拿法都要认：直接 `getElementById('x')`、经 `setText`/`setHidden`，
    以及 `for (const id of ['a', 'b'])` 那种循环——只认第一种的话，改用辅助函数
    的那一天这条用例会静静地什么都不再守。
    """
    ids = set(re.findall(r"(?:getElementById|setText|setHidden)\(\s*'([A-Za-z0-9_-]+)'", script))
    for block in re.findall(r"for \(const id of \[([^\]]+)\]\)", script):
        ids |= set(re.findall(r"'([A-Za-z0-9_-]+)'", block))
    return ids


def test_every_element_the_script_reaches_for_actually_exists(client: TestClient) -> None:
    """**这条用例就是钉那次事故的。**

    脚本去拿一个页面上没有的节点，在 JS 里不是「拿到空值」而是 `null`——
    往它身上写一个字就当场抛异常。删一个纯展示用的节点因此有能力把整块面板打红。
    """
    html = client.get("/missions").text
    declared = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))

    missing = sorted(_referenced_ids(_missions_script(html)) - declared)

    assert not missing, f"脚本要拿这些节点，页面上却没有：{missing}"


def test_the_log_path_node_is_back(client: TestClient) -> None:
    """日志尾巴只有 40 行，翻更早的只能靠这条路径——所以是把节点补回来，
    不是把那句赋值删掉。
    """
    html = client.get("/missions").text

    assert 'id="backfill-log-path"' in html
    assert "log_path" in _page_body(html)


def test_display_writes_go_through_the_forgiving_helper(client: TestClient) -> None:
    """补录那一段里不许再出现「拿到就写」的裸赋值。

    判据不是「这次补上了那个节点」——下一次紧凑化会删掉另一个。判据是**缺一个
    展示节点最多少显示一行字**，而那要靠 `setText` / `setHidden` 里那句判空。

    认的是「拿到就往上写」这个形状（`getElementById(...).随便什么`），
    不是「出现过 textContent」——先落一个局部变量、判过空再写，正是要提倡的写法。
    """
    body = _missions_script(client.get("/missions").text)
    start = body.index("function renderBackfill")
    block = body[start : body.index("function refreshBackfill")]

    chained = re.findall(r"getElementById\([^)]*\)\s*\.\s*[A-Za-z]", block)

    assert not chained, f"renderBackfill 里还有「拿到就写」的裸赋值：{chained}"
    assert "setText(" in block


def test_reading_the_state_never_moves_it(client: TestClient) -> None:
    """页面每 2 秒 `GET /api/backfill` 一次，渲染出错时也照旧在轮询。

    **读状态不许改状态。** 这条守的是「展示层出什么事都不该动到那一趟补录」的
    另一半：页面那一侧不发取消，服务端这一侧也不因为被反复问而改主意。
    """
    client.post("/api/backfill", json={"kind": "bot", "since": "2026-08-12"})
    before = client.get("/api/backfill").json()

    for _ in range(5):
        assert client.get("/api/backfill").json()["phase"] == before["phase"]

    assert before["phase"] != BackfillPhase.CANCELLED.value
