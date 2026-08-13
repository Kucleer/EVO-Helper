"""手动战报补录与启动对账的 HTTP 口。

状态机在 `tests/unit/application/test_backfill.py`，优先级与闸门在
`tests/integration/application/test_backfill_priority.py`。这里守的是**接口**：
参数怎么校验、什么情况下 409、页面轮询拿得到什么。

**这里不真的 Popen 任何东西**：任务与补录两侧的 `launch` 都注入假的。补录那一侧
尤其容易漏——它是第二个进程管理器，默认那份用的是真的 `subprocess.Popen`，而
「开始」默认会先排一批对账。
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from evo_helper.application.backfill import BackfillCoordinator, log_path_for
from evo_helper.application.mission_freeze import MissionFreezeLog
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app

#: 现实里的 2026-08-13 20:00（UTC+8），也就是那次事故的第二天。
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
YESTERDAY = "2026-08-12"
TOKEN = "test-token"


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exit_code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code if self.exit_code is not None else 0


class FakeLauncher:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.spawned: list[FakeProcess] = []

    def __call__(self, kind: MissionKind, command: Sequence[str], log_path: Path) -> FakeProcess:
        self.commands.append(tuple(command))
        process = FakeProcess(pid=9000 + len(self.spawned))
        self.spawned.append(process)
        return process


class FakeBackfillLauncher:
    """补录那一侧的假 `Popen`。签名少一个 `kind`——补录不是一条链路。"""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.spawned: list[FakeProcess] = []

    def __call__(self, command: Sequence[str], log_path: Path) -> FakeProcess:
        self.commands.append(tuple(command))
        process = FakeProcess(pid=8000 + len(self.spawned))
        self.spawned.append(process)
        return process

    @property
    def kinds(self) -> list[str]:
        return [command[command.index("--kind") + 1] for command in self.commands]


@dataclass
class Console:
    client: TestClient
    scheduler: MissionScheduler
    launcher: FakeLauncher
    backfill: FakeBackfillLauncher
    log_dir: Path

    def state(self) -> dict[str, object]:
        response = self.client.get("/api/backfill")
        assert response.status_code == 200, response.text
        body: dict[str, object] = response.json()
        return body

    def ask(self, **payload: object):  # type: ignore[no-untyped-def]
        body = {"kind": "pirate", "since": YESTERDAY} | payload
        return self.client.post("/api/backfill", json=body)

    def start(self, **payload: object):  # type: ignore[no-untyped-def]
        """点「开始」。**不带请求体 = 走真实默认（先对账）。**"""
        return self.client.post("/api/scheduler/start", json=payload or None)


@pytest.fixture
def console(tmp_path: Path) -> Iterator[Console]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'console.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    launcher = FakeLauncher()
    backfill = FakeBackfillLauncher()
    log_dir = tmp_path / "logs"
    scheduler = MissionScheduler(
        SqlAlchemyRepository(factory),
        MissionSupervisor(launch=launcher, clock=lambda: NOW, log_dir=log_dir),
        clock=lambda: NOW,
        freeze_log=MissionFreezeLog(tmp_path / "freezes.jsonl"),
        backfill=BackfillCoordinator(launch=backfill, clock=lambda: NOW, log_dir=log_dir),
    )
    app = create_persistent_app(
        factory,
        local_token=TOKEN,
        mission_scheduler=scheduler,
        # 后台 tick 先 sleep 再 tick，推到一小时就等于「测试期间不会自己跑」。
        tick_interval_s=3600.0,
    )
    with TestClient(app, headers={"X-Evo-Helper-Token": TOKEN}) as client:
        yield Console(client, scheduler, launcher, backfill, log_dir)


# -- 读 -------------------------------------------------------------------------


def test_nothing_is_running_to_begin_with(console: Console) -> None:
    body = console.state()

    assert body["phase"] == "未在补录"
    assert body["blocking"] is False
    assert body["summary"] is None


def test_an_idle_console_shows_no_leftover_log(console: Console) -> None:
    """上一次留下的日志尾巴摆在「未在补录」旁边，就是一段没有主语的输出。"""
    log_path_for("pirate", log_dir=console.log_dir).parent.mkdir(parents=True, exist_ok=True)
    log_path_for("pirate", log_dir=console.log_dir).write_text("上一次的输出", encoding="utf-8")

    assert console.state()["log_tail"] == ""


# -- 排一趟 ---------------------------------------------------------------------


def test_a_backfill_is_accepted_and_starts_when_the_window_is_free(console: Console) -> None:
    """**202 而不是 201**：这一下只是排上了。

    窗口可能还在海盗那一轮手上（那一轮不硬杀），所以「收下了」和「开跑了」
    必须分得开——回 201 会让人以为鼠标已经在动了。
    """
    response = console.ask()

    assert response.status_code == 202, response.text
    assert response.json()["phase"] == "补录中"
    assert console.backfill.kinds == ["pirate"]


def test_the_launched_command_carries_the_kind_and_the_since(console: Console) -> None:
    """命令行契约。多一个参数不是「多余」，是这条链路起不来（argparse 会
    `SystemExit(2)`），而页面上只会显示「补录失败」。
    """
    console.ask(kind="bot", since="2026-08-11")

    command = console.backfill.commands[0]
    assert command[command.index("--kind") + 1] == "bot"
    assert command[command.index("--since") + 1] == "2026-08-11"
    assert command[-1] != "--attack"  # 补录只读，命令行上不该有任何派遣开关


def test_the_button_can_reach_reports_that_fell_off_the_worklist(console: Console) -> None:
    """**页面上那个按钮默认要跑补录模式，不是对账模式。**

    它最主要的用途就是救过期战报（2026-08-12 那夜丢的 21 份），而那些派遣早就
    掉出了 `due_attack_dispatches` 的 6 小时窗口——单子从头到尾是空的，对账模式
    撞见第一封「库里已有」就收工，**一份都够不着，跑完还显示「补录完成」**。

    这条差点漏掉：命令契约是在 CLI 长出 `--exhaustive` 之前定下的，于是这个按钮
    一度只会跑对账模式，而当时全套测试是绿的。
    """
    console.ask(kind="bot", since=YESTERDAY)

    assert "--exhaustive" in console.backfill.commands[0]


def test_an_unknown_chain_is_refused(console: Console) -> None:
    assert console.ask(kind="scan").status_code == 400
    assert console.backfill.commands == []


def test_a_future_start_date_is_refused(console: Console) -> None:
    """那趟信箱翻下来必然一封都不匹配，而它要占着游戏窗口十几分钟，跑完还显示
    「补录完成」——一句看着正常的假话。
    """
    response = console.ask(since="2026-09-01")

    assert response.status_code == 400
    assert "未来" in response.json()["detail"]
    assert console.backfill.commands == []


def test_today_itself_is_still_allowed(console: Console) -> None:
    """拦的是「在未来」，不是「不是昨天」：今天出的事当天补回来是正常需求。"""
    assert console.ask(since="2026-08-13").status_code == 202


def test_the_same_chain_cannot_be_queued_twice(console: Console) -> None:
    console.ask()

    response = console.ask()

    assert response.status_code == 409
    assert len(console.backfill.commands) == 1


def test_the_other_chain_queues_up_behind_it(console: Console) -> None:
    console.ask(kind="pirate")

    response = console.ask(kind="bot")

    assert response.status_code == 202
    assert response.json()["queued"] == 1
    assert console.backfill.kinds == ["pirate"]


# -- 反过来：补录跑着时不许启动调度器 -------------------------------------------


def test_the_scheduler_cannot_be_started_while_a_backfill_runs(console: Console) -> None:
    """一个游戏窗口，一只鼠标。

    起任务那道闸门在调度器里（`tests/integration/application/test_backfill_priority.py`），
    这一层拦得更早：让用户点下去、看着调度器开着却一个任务都不起，比当场说明白
    糟得多。
    """
    console.ask()

    response = console.start()

    assert response.status_code == 409
    assert "补录" in response.json()["detail"]
    assert console.client.get("/api/scheduler").json()["running"] is False


def test_starting_again_while_already_running_is_not_refused(console: Console) -> None:
    """拦的是「停 → 开」那一次跃迁。

    调度器已经开着时这一下本来就是空操作，而启动对账恰恰是它自己排出来的——
    不区分的话，点一次「开始」之后再点一次会被自己排的那批补录 409 掉。
    """
    assert console.start().status_code == 200

    assert console.start().status_code == 200


# -- 启动对账 -------------------------------------------------------------------


def test_pressing_start_reconciles_by_default(console: Console) -> None:
    """**默认是做。** 用户口径：「启动调度台之后，先检查有多少应读未读战报」。

    不带请求体也要对账：桌面悬浮窗和文档 4.3 节里那条 curl 都是不带体打的。
    """
    assert console.start().status_code == 200

    assert console.backfill.kinds == ["pirate"]
    assert console.state()["queued"] == 1
    assert console.state()["reason"] == "启动对账"


def test_an_empty_body_also_reconciles(console: Console) -> None:
    """**两个默认值，两条路。**

    不带请求体走的是路由那个默认（悬浮窗与文档里那条 curl 都是这条）；带一个
    `{}` 走的是 `SchedulerStartIn` 里那个默认。两处任意一个写成 False，都会让
    「点开始先对账」在某一条路上悄悄失效。
    """
    assert console.client.post("/api/scheduler/start", json={}).status_code == 200

    assert console.backfill.kinds == ["pirate"]


def test_the_reconciliation_can_be_skipped_on_purpose(console: Console) -> None:
    """「刚停了两分钟又点开始、明知没有欠账」那条口子。"""
    assert console.start(reconcile=False).status_code == 200

    assert console.backfill.commands == []
    assert console.state()["phase"] == "未在补录"


def test_the_startup_reconciliation_starts_from_yesterday(console: Console) -> None:
    """**默认「今天」会把昨夜漏掉的那批整批藏起来**，而漏掉的恰恰是昨夜的。

    游戏时间按 UTC+0 显示，UTC 的今天要到现实时间早上 8 点才开始。
    """
    console.start()

    command = console.backfill.commands[0]
    assert command[command.index("--since") + 1] == YESTERDAY


def test_a_manual_backfill_that_already_finished_never_blocks_start(console: Console) -> None:
    """「先手动补海盗、再点开始」是一条正常的路，不该把「开始」整个打成错误。

    海盗那趟**会再跑一遍**，这是有意的：两次之间可能又来了新战报，而那一趟
    信箱单子一空就早停，代价是几十秒。反过来「跳过刚补过的链路」才危险——
    刚补完到点开始之间隔了多久，没有人知道。

    还有一层：手动那一趟跑完还没确认时，任务本来就被扣着；点「开始」顺手把它
    确认掉，否则用户会撞上一台开着却一个任务都不起的调度器。
    """
    console.ask(kind="pirate")
    console.backfill.spawned[0].exit_code = 0
    console.scheduler.tick()
    assert console.state()["awaiting_ack"] is True

    assert console.start().status_code == 200

    assert console.backfill.kinds == ["pirate", "pirate"]
    assert console.state()["reason"] == "启动对账"


# -- 跑完：摘要与放行 -----------------------------------------------------------


def test_a_finished_backfill_reports_what_it_changed_and_waits(console: Console) -> None:
    """**跑完不自动放行。** 用户要凭这几个数决定放不放行。"""
    console.ask()
    console.backfill.spawned[0].exit_code = 0
    console.scheduler.tick()

    body = console.state()

    assert body["phase"] == "补录完成"
    assert body["awaiting_ack"] is True
    assert body["blocking"] is True
    assert body["summary"] == {
        "reports_ingested": 0,
        "dispatches_claimed": 0,
        "bot_targets_settled": 0,
        "bot_targets_measured": 0,
    }


def test_resuming_releases_the_tasks_and_keeps_the_summary(console: Console) -> None:
    console.ask()
    console.backfill.spawned[0].exit_code = 0
    console.scheduler.tick()

    body = console.client.post("/api/backfill/resume").json()

    assert body["blocking"] is False
    assert body["awaiting_ack"] is False
    assert body["summary"] is not None
    assert body["phase"] == "补录完成"


def test_a_failed_backfill_says_so_and_still_holds_the_tasks(console: Console) -> None:
    """失败尤其不能自动放行：那意味着数据仍然不全。"""
    console.ask()
    console.backfill.spawned[0].exit_code = 1
    console.scheduler.tick()

    body = console.state()

    assert body["phase"] == "补录失败"
    assert body["exit_code"] == 1
    assert body["blocking"] is True


def test_cancelling_stops_it_and_releases_the_tasks(console: Console) -> None:
    console.ask()

    body = console.client.post("/api/backfill/cancel").json()

    assert console.backfill.spawned[0].terminated is True
    assert body["phase"] == "已取消"
    assert body["blocking"] is False


# -- 进度 -----------------------------------------------------------------------


def test_the_log_tail_is_served_while_it_runs(console: Console) -> None:
    """补录最坏要跑十几分钟（60 封 × 约 15 秒），按钮点下去之后页面不能只是
    「没反应」，而进度的唯一来源就是这份日志。
    """
    console.ask()
    path = log_path_for("pirate", log_dir=console.log_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("已读 3 / 21 封\n已入库 2 份\n", encoding="utf-8")

    body = console.state()

    assert "已入库 2 份" in str(body["log_tail"])
    assert "backfill-pirate.log" in str(body["log_path"])


def test_a_queued_backfill_says_which_round_it_is_waiting_for(console: Console) -> None:
    """页面上只写「等任务结束」的话，用户看到的是一个半小时不动的状态条，
    而它其实完全正常——海盗那一轮就是要跑那么久，硬杀它会留下没记账的派遣。
    """
    tasks = console.client.get("/api/scheduler").json()["tasks"]
    pirate = next(item for item in tasks if item["kind"] == "PIRATE")
    console.client.patch(f"/api/missions/{pirate['task_id']}", json={"enabled": True})
    console.start(reconcile=False)
    console.scheduler.tick()

    console.ask()

    body = console.state()
    assert body["phase"] == "等任务结束"
    assert "侦查+攻击海盗" in str(body["detail"])
    assert console.backfill.commands == []


# -- 写接口的鉴权 ---------------------------------------------------------------


def test_the_write_endpoints_need_the_token_or_a_same_origin_request(tmp_path: Path) -> None:
    """写接口一律要带 token（见 `web/security.py` 与部署文档 4.3 节）。

    补录会真的动鼠标翻信箱，它绝不该是三个写接口里唯一敞着的那个。

    ⚠️ 这一条也得用**注入了假 launcher 的**调度器，哪怕它断言的是 403：默认那份
    协调器用的是真的 `subprocess.Popen`，一旦这道鉴权真的破了（或者有人在这里做
    变异测试），这条用例就会亲手起一个去翻信箱的补录进程。实测跑变异 M24 时它就
    在工作区里落下了一份 `var/logs/backfill-pirate.log`。
    """
    engine = create_database_engine(f"sqlite:///{tmp_path / 'auth.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    app = create_persistent_app(
        factory,
        local_token=TOKEN,
        mission_scheduler=MissionScheduler(
            SqlAlchemyRepository(factory),
            MissionSupervisor(launch=FakeLauncher(), clock=lambda: NOW, log_dir=tmp_path / "logs"),
            clock=lambda: NOW,
            freeze_log=MissionFreezeLog(tmp_path / "freezes.jsonl"),
            backfill=BackfillCoordinator(
                launch=FakeBackfillLauncher(), clock=lambda: NOW, log_dir=tmp_path / "logs"
            ),
        ),
        tick_interval_s=3600.0,
    )

    with TestClient(app) as client:  # 不带 token，也没有 Origin
        for path in ("/api/backfill", "/api/backfill/cancel", "/api/backfill/resume"):
            response = client.post(path, json={"kind": "pirate", "since": YESTERDAY})
            assert response.status_code == 403, path
        # 读接口不受影响。
        assert client.get("/api/backfill").status_code == 200


def test_the_token_lets_the_backfill_through(console: Console) -> None:
    """反面：带上 token 就该放行，否则上一条测的可能只是「这个接口坏了」。"""
    assert console.ask().status_code == 202


# -- 时钟 -----------------------------------------------------------------------


def test_the_started_at_is_the_schedulers_clock(console: Console) -> None:
    """页面上那块秒表从这里起算。取两次 `now()` 的话，秒表会和日志对不上。"""
    console.ask()

    started = console.state()["started_at_utc"]

    assert isinstance(started, str)
    assert started.startswith(NOW.strftime("%Y-%m-%dT%H:%M"))
    assert (NOW + timedelta(0)).tzinfo is UTC
