"""调度台的 API。

写请求只有同源校验（局域网内浏览器天然同源），所以这里只测行为，不测鉴权——
那是 `web/security.py` 的事。

**这里不真的 Popen 任何 runner**：`launch` 一律注入假的。真起一个会去点用户的
真实鼠标、派真实舰队。后台 tick 也被推到一小时一次，免得测试里冒出计划外的启动。

⚠️ **补录那个协调器也要注入假的 `launch`。** 它是第二个进程管理器，默认那份用
的是真的 `subprocess.Popen`——点「开始」默认会先排一批对账，漏了这一下，
`pytest` 会真的去起 `evo_helper.tools.backfill_reports`（实测在工作区里落下了一份
`var/logs/backfill-pirate.log`）。
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from evo_helper.application.backfill import BackfillCoordinator
from evo_helper.application.mission_freeze import DEFAULT_FREEZE_LOG, MissionFreezeLog
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    TARGET_KIND_BOT,
    AttackDispatch,
    AttackIntent,
    FleetPresetRef,
)
from evo_helper.domain.scheduler import MissionKind
from evo_helper.storage import models as orm
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app
from support.database import scratch_database_url

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
TOKEN = "test-token"


class FakeProcess:
    """`subprocess.Popen` 里 supervisor 用到的那一小部分。"""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.exit_code: int | None = None

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.exit_code = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code if self.exit_code is not None else 0


class FakeLauncher:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, kind: MissionKind, command: Sequence[str], log_path: Path) -> FakeProcess:
        self.commands.append(tuple(command))
        return FakeProcess(pid=9000 + len(self.commands))


class FakeBackfillLauncher:
    """补录那一侧的假 `Popen`。签名少一个 `kind`——补录不是一条链路。"""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.processes: list[FakeProcess] = []

    def __call__(self, command: Sequence[str], log_path: Path) -> FakeProcess:
        self.commands.append(tuple(command))
        process = FakeProcess(pid=8000 + len(self.commands))
        self.processes.append(process)
        return process


class MovableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Console:
    client: TestClient
    repository: SqlAlchemyRepository
    scheduler: MissionScheduler
    launcher: FakeLauncher
    clock: MovableClock
    #: 配置固化记录落盘的地方。**临时目录**，测试不许往仓库里写文件。
    freeze_log: Path
    backfill_launcher: FakeBackfillLauncher

    def start(self, *, reconcile: bool = False):  # type: ignore[no-untyped-def]
        """点「开始」。**这些用例默认跳过启动对账。**

        对账本身是真实默认（`reconcile` 不给就是做，见 `SchedulerStartIn`），
        但它会先扣住窗口——本节这些用例说的是「调度器起不起得了任务」，带上对账
        的话每一条都得先把那批补录走完，测的东西就从判据变成了补录。对账那几条
        单独在 `test_backfill_api.py` 里。
        """
        return self.client.post("/api/scheduler/start", json={"reconcile": reconcile})

    def get(self) -> dict[str, object]:
        response = self.client.get("/api/scheduler")
        assert response.status_code == 200, response.text
        body: dict[str, object] = response.json()
        return body

    def task(self, kind: str) -> dict[str, object]:
        tasks = self.get()["tasks"]
        assert isinstance(tasks, list)
        for item in tasks:
            if item["kind"] == kind:
                found: dict[str, object] = item
                return found
        raise AssertionError(f"调度台上没有 {kind} 这一行")

    def task_id(self, kind: str) -> int:
        """这条链路那一行的 id。

        接口按 **id** 寻址（同一 kind 可以有多行），而这些用例说的是「海盗那一
        行」「bot 那一行」——种子行每条链路各一个，从状态接口里把 id 捞出来即可。
        """
        return int(self.task(kind)["task_id"])  # type: ignore[arg-type]

    def patch(self, kind: str, payload: dict[str, object]):  # type: ignore[no-untyped-def]
        return self.client.patch(f"/api/missions/{self.task_id(kind)}", json=payload)

    def new_round(self, kind: str = "BOT"):  # type: ignore[no-untyped-def]
        return self.client.post(f"/api/missions/{self.task_id(kind)}/new-round")


def _seed_bot(repository: SqlAlchemyRepository, coordinate: Coordinate) -> None:
    """往 `bot_targets` 里放一颗已记录的 bot。

    「范围内有没有 bot」是启用 bot 链路的硬前提，所以它必须来自真实的库，
    不能靠打桩——打桩的话，这条判据断了测试也不会红。

    **军力分数和读取时刻两样都给**：用户 2026-08-18 决定，从没上过军力榜的目标
    不再攻击（`domain.target_order.has_a_military_reading`），只给时刻不给分数的话
    这颗目标压根进不了候选池，而那与用例本身要验的事情无关。
    """
    with repository._session_factory() as session:  # noqa: SLF001 - 测试直接落库
        session.add(
            orm.BotTargetRow(
                galaxy=coordinate.galaxy,
                system=coordinate.system,
                position=coordinate.position,
                is_bot=True,
                latest_owner_name="bot",
                last_scanned_at_utc=NOW,
                military_score=9_000.0,
                military_score_at_utc=NOW,
            )
        )
        session.commit()


@pytest.fixture
def console(tmp_path: Path) -> Iterator[Console]:
    engine = create_database_engine(scratch_database_url(tmp_path, "console.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    repository = SqlAlchemyRepository(factory)
    clock = MovableClock(NOW)
    launcher = FakeLauncher()
    supervisor = MissionSupervisor(launch=launcher, clock=clock, log_dir=tmp_path / "logs")
    freeze_log = tmp_path / "freezes.jsonl"
    backfill_launcher = FakeBackfillLauncher()
    scheduler = MissionScheduler(
        repository,
        supervisor,
        clock=clock,
        freeze_log=MissionFreezeLog(freeze_log),
        backfill=BackfillCoordinator(
            launch=backfill_launcher, clock=clock, log_dir=tmp_path / "logs"
        ),
    )
    app = create_persistent_app(
        factory,
        local_token=TOKEN,
        mission_scheduler=scheduler,
        # 后台 tick 先 sleep 再 tick，推到一小时就等于「测试期间不会自己跑」。
        tick_interval_s=3600.0,
    )
    with TestClient(app, headers={"X-Evo-Helper-Token": TOKEN}) as client:
        yield Console(client, repository, scheduler, launcher, clock, freeze_log, backfill_launcher)


# -- 手动清理航线占用 -----------------------------------------------------------
#
# 库里的航线占用是**推算**出来的（出发时刻 + 派出时读到的飞行时长 × 倍数），
# 舰队真回港了它也不会自己改口。用户口径 2026-08-16：「时间到了，自然就释放了
# 航线，我会手动 check 后清理。」


def _seed_inflight(console: Console, *, origin: Coordinate, position: int) -> None:
    """往库里放一支「还在外面没回来」的舰队。

    走真实的 `save_attack_intent` / `save_dispatch` / `record_flight_time`，
    不直接拼 ORM 行：航线占用是由这三步共同算出来的，绕开它们等于让这几条用例
    去验一份自己捏出来的状态。
    """
    with console.repository._session_factory() as session:  # noqa: SLF001 - 测试直接落库
        plan = orm.ScanPlan(name=f"lines-{position}", created_at_utc=NOW)
        session.add(plan)
        session.flush()
        run = orm.RunInstance(
            plan_id=plan.id,
            idempotency_key=f"lines-{position}",
            state="SCANNING",
            created_at_utc=NOW,
        )
        session.add(run)
        session.commit()
        run_id = run.id

    intent_id = uuid4()
    console.repository.save_attack_intent(
        AttackIntent(
            intent_id=intent_id,
            run_id=run_id,
            origin=origin,
            target=Coordinate(2, 137, position),
            preset=FleetPresetRef(name="AAA", signature="sig"),
            cycle_start_utc=NOW,
            created_at_utc=NOW,
            target_kind=TARGET_KIND_BOT,
        )
    )
    dispatch_id = uuid4()
    console.repository.save_dispatch(
        AttackDispatch(
            dispatch_id=dispatch_id,
            intent_id=intent_id,
            dispatched_at_utc=NOW,
            accepted=True,
        )
    )
    # 攻击发按 2× 算：这一发要到 NOW + 2 小时才自然放手。
    console.repository.record_flight_time(dispatch_id, timedelta(hours=1), NOW)


def test_releasing_the_lines_frees_them_and_says_how_many(console: Console) -> None:
    """点一下「清理航线占用」，占用真的没了，回执给出放开的条数。

    条数要报出来：这个按钮唯一的可见后果是若干个任务从「等航线」变回「待命」，
    而那要等下一轮轮询才看得见。中间这段空白里，这个数字是用户判断
    「点到了没有」的唯一凭据。
    """
    home = Coordinate(2, 137, 18)
    _seed_inflight(console, origin=home, position=71)
    _seed_inflight(console, origin=home, position=72)
    assert console.repository.count_inflight(now_utc=NOW, origin=home) == 2

    response = console.client.post("/api/attack-lines/release")

    assert response.status_code == 200, response.text
    assert response.json()["released"] == 2
    assert console.repository.count_inflight(now_utc=NOW, origin=home) == 0


def test_releasing_the_lines_needs_the_token_or_a_same_origin_request(console: Console) -> None:
    """**没有凭据的请求必须被拒，而且一条航线都不许放开。**

    这一下的后果是真实舰队出港、烧真实燃料，它绝不该是写接口里唯一敞着的那个。

    断言不止看 403：光看状态码的话，一个「先放手再拒」的实现照样是绿的。

    ⚠️ 这里靠**请求级的错误令牌**盖掉 `console` 那个客户端头上带的正确令牌
    （httpx 同名头按请求覆盖），同时不带 `Origin`——两条放行路径一起断掉。
    """
    home = Coordinate(2, 137, 18)
    _seed_inflight(console, origin=home, position=73)

    response = console.client.post(
        "/api/attack-lines/release", headers={"X-Evo-Helper-Token": "wrong-token"}
    )

    assert response.status_code == 403, response.text
    assert console.repository.count_inflight(now_utc=NOW, origin=home) == 1


# -- 读 -------------------------------------------------------------------------


def test_the_scheduler_starts_stopped(console: Console) -> None:
    """控制台重启后一律停在「已停止」。重启多半意味着出了事。"""
    body = console.get()

    assert body["running"] is False
    assert body["current"] is None
    assert body["started_at_utc"] is None


def test_every_task_is_listed_in_priority_order(console: Console) -> None:
    body = console.get()
    tasks = body["tasks"]
    assert isinstance(tasks, list)

    assert sorted(item["kind"] for item in tasks) == ["BOT", "PIRATE", "RANKING", "SCAN"]
    priorities = [item["priority"] for item in tasks]
    assert priorities == sorted(priorities)


def test_every_task_carries_a_status_and_a_detail(console: Console) -> None:
    """页面不查库：状态那句话必须由这个接口给全。"""
    assert console.task("SCAN")["status"] == "待命"
    assert console.task("PIRATE")["status"] == "未启用"
    assert console.task("BOT")["status"] == "未启用"


def test_the_pirate_row_echoes_the_systems_its_radius_covers(console: Console) -> None:
    """半径 10 是多大范围，用户心里没数；回显出来才看得见填错没有。"""
    summary = console.task("PIRATE")["summary"]
    assert isinstance(summary, str)
    assert "2:127" in summary
    assert "2:147" in summary
    assert "21" in summary


def test_the_bot_row_echoes_how_many_bots_the_range_holds(console: Console) -> None:
    """N=0 就禁止启用，所以 N 必须先看得见。"""
    _seed_bot(console.repository, Coordinate(2, 150, 5))
    console.patch(
        "BOT",
        {"params": {"galaxy": 2, "first_system": 100, "last_system": 200}},
    )

    summary = console.task("BOT")["summary"]
    assert isinstance(summary, str)
    assert "1" in summary


def test_the_range_bot_row_reports_how_many_of_this_round_are_left(console: Console) -> None:
    """范围模式下这个数是真的本轮进度：范围里那几个目标，还有几个没走完。

    与下一条成对——改坏「军力模式不报数」时，这条要保持绿，否则说明连范围模式
    那句对的话也一起删掉了。
    """
    _seed_bot(console.repository, Coordinate(2, 150, 5))
    console.patch(
        "BOT",
        {"params": {"galaxy": 2, "first_system": 100, "last_system": 200}, "enabled": True},
    )

    assert console.task("BOT")["detail"] == "还剩 1 个未完成"


def test_the_military_bot_row_never_claims_a_round_progress(console: Console) -> None:
    """军力优先模式下不报「还剩 N 个未完成」——那个 N 不是本轮进度。

    军力模式没有「本轮范围」：`targets_remaining` 走的是 `_military_candidates`，
    数的是**全库**还能打的 bot（排除近 24 小时打过的），实机两千多个，而任务
    每轮只按得分派出航线预算允许的那几发。把它写成「还剩 N 个未完成」，用户会当成本轮进度盯着
    它往下走，可两个数从来对不上。用户口径 2026-08-17：「那个剩余 2098 就不需要
    显示」。

    这里断言的是**整句为空**而不是「不含某个数字」：后者用「还剩 2 个未完成」
    照样能过（2 这个数字它不找），等于什么都没验。
    """
    _seed_bot(console.repository, Coordinate(2, 150, 5))
    _seed_bot(console.repository, Coordinate(2, 151, 6))
    console.patch("BOT", {"params": {"by_military": True, "top_n": 1}, "enabled": True})

    row = console.task("BOT")
    # 先确认它真的在参与调度：状态是「未启用」的话，`_detail` 早在 BOT 那一档
    # 之前就返回空串了，这条用例会因为一个完全无关的原因变绿。
    assert row["status"] != "未启用"
    assert row["detail"] == ""


# -- 开始 / 结束 -----------------------------------------------------------------


def test_starting_and_stopping_flips_the_flag(console: Console) -> None:
    assert console.start().status_code == 200
    assert console.get()["running"] is True

    assert console.client.post("/api/scheduler/stop").status_code == 200
    assert console.get()["running"] is False


def test_the_running_child_is_reported_with_its_log(console: Console) -> None:
    """页面要显示「当前跑的是哪条链路、已运行多久」，两样都从这里取。"""
    console.start()
    console.scheduler.tick()
    console.clock.now = NOW + timedelta(minutes=2)

    body = console.get()
    current = body["current"]
    assert isinstance(current, dict)
    assert current["kind"] == "SCAN"
    assert current["started_at_utc"].startswith("2026-08-09T12:00")
    assert "mission-scan.log" in current["log_path"]
    assert console.task("SCAN")["status"] == "运行中"


def test_stopping_kills_the_child(console: Console) -> None:
    console.start()
    console.scheduler.tick()
    assert console.get()["current"] is not None

    console.client.post("/api/scheduler/stop")

    assert console.get()["current"] is None


# -- PATCH ----------------------------------------------------------------------


def test_priority_can_be_reordered(console: Console) -> None:
    assert console.patch("BOT", {"priority": -1}).status_code == 200

    tasks = console.get()["tasks"]
    assert isinstance(tasks, list)
    assert tasks[0]["kind"] == "BOT"


def test_the_scan_priority_cannot_be_written(console: Console) -> None:
    """扫描恒在最后一位。

    领域层的排序键已经结构性地保证了这一点，所以接受一个 priority 写入不会
    真的改变次序——正因为如此才必须拒绝：默默收下一个不起作用的值，页面会
    显示成「排序已保存」，刷新后又弹回去，用户只能得出「这个控件坏了」。
    """
    response = console.patch("SCAN", {"priority": 0})

    assert response.status_code == 400
    assert "扫描" in response.json()["detail"]
    tasks = console.get()["tasks"]
    assert isinstance(tasks, list)
    # 填空隙的两种都结构性地排在最后，它们之间再按 priority（SCAN 2 < RANKING 3）。
    assert [item["kind"] for item in tasks[-2:]] == ["SCAN", "RANKING"]


def test_the_scan_row_can_still_be_switched_off(console: Console) -> None:
    """挡的只是 priority 那一个字段，别把整行改成只读。"""
    response = console.patch("SCAN", {"enabled": False})

    assert response.status_code == 200
    assert console.task("SCAN")["enabled"] is False


def test_a_bot_range_with_no_recorded_bots_is_refused(console: Console) -> None:
    """拉起一个必然空转的 runner 没有意义，早一步告诉用户。"""
    response = console.patch(
        "BOT",
        {"enabled": True, "params": {"galaxy": 9, "first_system": 1, "last_system": 2}},
    )

    assert response.status_code == 400
    assert "没有已记录的 bot" in response.json()["detail"]
    assert console.task("BOT")["enabled"] is False


def test_enabling_a_bot_range_that_holds_bots_is_accepted(console: Console) -> None:
    _seed_bot(console.repository, Coordinate(2, 150, 5))

    response = console.patch(
        "BOT",
        {"enabled": True, "params": {"galaxy": 2, "first_system": 100, "last_system": 200}},
    )

    assert response.status_code == 200
    assert console.task("BOT")["enabled"] is True


def test_enabling_without_params_still_checks_the_stored_ones(console: Console) -> None:
    """勾复选框那一下也要过校验——否则先存一个空范围、再单独勾上就绕过去了。"""
    response = console.patch("BOT", {"enabled": True})

    assert response.status_code == 400
    assert console.task("BOT")["enabled"] is False


def test_a_non_positive_pirate_radius_is_refused(console: Console) -> None:
    response = console.patch("PIRATE", {"params": {"radius": 0}})

    assert response.status_code == 400


def test_a_reversed_system_range_is_refused(console: Console) -> None:
    response = console.patch(
        "BOT",
        {"params": {"galaxy": 2, "first_system": 200, "last_system": 100}},
    )

    assert response.status_code == 400
    assert "颠倒" in response.json()["detail"]


def test_switching_a_task_off_never_needs_valid_params(console: Console) -> None:
    """关一条链路必须永远做得到。参数填错了还关不掉，那就真的没退路了。"""
    response = console.patch("BOT", {"enabled": False})

    assert response.status_code == 200


def test_patching_clears_an_automatic_disable(console: Console) -> None:
    """参数填错一次、改好了也永远起不来，是最容易踩的那个坑。"""
    console.repository.disable_mission_task(console.task_id("PIRATE"), "连续 3 次异常退出")

    console.patch("PIRATE", {"params": {"radius": 5}})

    assert console.task("PIRATE")["disabled_reason"] is None


def test_a_disabled_scan_is_revived_by_enabling_it_again(console: Console) -> None:
    """**页面上那个「恢复」按钮走的就是这一条。**

    扫描不吃参数、也不许改优先级，所以它只有 `enabled` 这一条改得动的路；
    而自动停用时 `enabled` 本来就还是 True——不认这一下的话，一条被
    「连续 3 次异常退出」停掉的扫描在页面上永远没有恢复的办法，用户只能去改库。
    计数也必须一起清零，否则下一次崩溃立刻又满三次。
    """
    console.repository.record_mission_failure(console.task_id("SCAN"), exit_code=1, limit=1)
    assert console.task("SCAN")["disabled_reason"] is not None

    response = console.patch("SCAN", {"enabled": True})

    assert response.status_code == 200, response.text
    assert console.task("SCAN")["disabled_reason"] is None
    assert console.task("SCAN")["status"] != "已停用"
    row = next(row for row in console.repository.mission_tasks() if row.kind == "SCAN")
    assert row.consecutive_failures == 0


def test_an_unknown_kind_is_a_404(console: Console) -> None:
    assert console.client.patch("/api/missions/9999", json={"enabled": True}).status_code == 404


# -- 定时开启 / 定时关闭 --------------------------------------------------------
#
# 用户口径（2026-08-17）：每个任务可以设一个开启时刻和一个关闭时刻，到点自动生效。
# 绝对时刻、一次性。页面上填的是 **UTC+8** 的墙上时钟，库里存 UTC。


def test_a_schedule_window_round_trips_through_the_api(console: Console) -> None:
    """页面送带 `+08:00` 的时刻，接口回的是同一时刻的 UTC 写法。

    ⚠️ 这里刻意用 **UTC+8 的 08:00**（= UTC 前一天 00:00）：它跨了日期边界，
    所以「偏移量被当成装饰品直接丢掉」那种错在这一条上必然露馅——丢掉的话回来的
    是同一天的 08:00，而正确答案是前一天的 00:00。
    """
    response = console.patch("BOT", {"enabled_from": "2026-08-17T08:00:00+08:00"})

    assert response.status_code == 200, response.text
    assert console.task("BOT")["enabled_from_utc"] == "2026-08-17T00:00:00Z"


def test_a_moment_without_a_timezone_is_refused(console: Console) -> None:
    """不带时区一律 400。

    服务端替它猜一个的代价正好是 8 小时——一个「说好 22 点开、实际 14 点就开了」
    的错，且全程不报任何异常。本仓库已经被时区坑过三次。
    """
    response = console.patch("BOT", {"enabled_from": "2026-08-17T08:00:00"})

    assert response.status_code == 400, response.text
    assert "时区" in response.json()["detail"]


def test_clearing_one_end_leaves_the_other_end_alone(console: Console) -> None:
    """空串是「把这一端退回不限」，而且**只动这一端**。

    两端合起来存的话，只清开启时刻会把关闭时刻一起抹掉——那是一个「本以为到点
    会停、结果一直跑下去」的错，而页面上看不出任何异样。
    """
    console.patch("BOT", {"enabled_from": "2026-08-17T20:00:00+08:00"})
    console.patch("BOT", {"enabled_until": "2026-08-17T23:00:00+08:00"})

    assert console.patch("BOT", {"enabled_from": ""}).status_code == 200

    assert console.task("BOT")["enabled_from_utc"] is None
    assert console.task("BOT")["enabled_until_utc"] == "2026-08-17T15:00:00Z"


def test_patching_something_else_does_not_wipe_the_window(console: Console) -> None:
    """`None`（这次不动它）与 `""`（清空）必须分得开。

    分不开的话，任何一次只改优先级的 PATCH——包括页面上那个拖拽把手每拖一次
    发的一串——都会顺手把定时窗口抹掉。
    """
    console.patch("BOT", {"enabled_until": "2026-08-17T23:00:00+08:00"})

    console.patch("BOT", {"name": "改个名字"})

    assert console.task("BOT")["enabled_until_utc"] == "2026-08-17T15:00:00Z"


def test_a_stop_time_that_is_not_after_the_start_time_is_refused(console: Console) -> None:
    """区间是左闭右开的，`until <= from` 表示一个空区间。

    收下它的话，那个任务永远起不来，而页面上只会写「未到开启时间」——一句用户
    照着等、等到关闭时刻也不会动的话。
    """
    console.patch("BOT", {"enabled_from": "2026-08-17T20:00:00+08:00"})

    response = console.patch("BOT", {"enabled_until": "2026-08-17T20:00:00+08:00"})

    assert response.status_code == 400, response.text
    assert console.task("BOT")["enabled_until_utc"] is None


def test_a_task_before_its_start_time_says_why_it_is_standing_still(console: Console) -> None:
    """状态列必须说出原因，不能笼统地写「待命」。

    2026-08-16 晚上刚发生过「任务不动而界面不说原因、查了一小时」的事。
    """
    console.patch("SCAN", {"enabled_from": "2026-08-17T08:00:00+08:00"})

    assert console.task("SCAN")["status"] == "未到开启时间"


def test_a_task_past_its_stop_time_says_why_it_is_standing_still(console: Console) -> None:
    console.patch("SCAN", {"enabled_until": "2026-08-09T19:59:00+08:00"})

    assert console.task("SCAN")["status"] == "已过关闭时间"


def test_the_window_cannot_be_changed_while_the_scheduler_runs(console: Console) -> None:
    """定时窗口和别的配置同档：跑着的时候一样改不动。

    它是判据的一部分，改了会立刻生效到下一轮，而上一轮正拿着旧口径在飞——
    那正是固化要挡的那件事。
    """
    assert console.start().status_code == 200

    response = console.patch("BOT", {"enabled_until": "2026-08-17T23:00:00+08:00"})

    assert response.status_code == 409, response.text
    assert console.task("BOT")["enabled_until_utc"] is None


# -- 运行中不许改 ---------------------------------------------------------------
#
# 用户口径（2026-08-11）：「任务开始后，调度台固化任务数据，记录任务内容。
# 并且开始后，无法修改任务，只有结束状态才可以修改」。
#
# 为什么必须拒而不是收下：`_step()` 每秒重新去库里读一遍配置，收下的改动会
# **立刻**生效到下一轮，而上一轮正拿着旧参数在飞。一轮之内两套口径，事后从
# `mission_runs` 里只看得到一行命令行，分不出当时用的是哪一套。


def _start(console: Console) -> None:
    assert console.start().status_code == 200


def test_params_cannot_be_changed_while_the_scheduler_runs(console: Console) -> None:
    console.patch("PIRATE", {"params": {"radius": 5}})
    _start(console)

    response = console.patch("PIRATE", {"params": {"radius": 30}})

    assert response.status_code == 409, response.text
    assert "运行中" in response.json()["detail"]
    # 拒了就得真的没改。收下一个 409 却把值写进去，比静默忽略更糟。
    assert console.task("PIRATE")["params"] == {"radius": 5}


def test_priority_cannot_be_reordered_while_the_scheduler_runs(console: Console) -> None:
    """拖拽也走这个 PATCH，所以这一条同时守住了那个拖拽把手。"""
    _start(console)

    response = console.patch("BOT", {"priority": -1})

    assert response.status_code == 409
    tasks = console.get()["tasks"]
    assert isinstance(tasks, list)
    assert tasks[0]["kind"] != "BOT"


def test_a_chain_cannot_be_switched_off_while_the_scheduler_runs(console: Console) -> None:
    """复选框也是任务配置的一部分：中途摘掉一条链路同样是「一轮之内两套口径」。"""
    _start(console)

    response = console.patch("SCAN", {"enabled": False})

    assert response.status_code == 409
    assert console.task("SCAN")["enabled"] is True


def test_a_disabled_chain_can_still_be_revived_while_the_scheduler_runs(
    console: Console,
) -> None:
    """**「恢复」是运行中唯一的口子。**

    一条链路完全可能在调度器跑着的时候被自动停用（连崩三次，多半是「窗口抢不到
    前台」这类环境原因），而那正是用户最需要把它恢复回来的时刻。一刀切禁掉 PATCH
    的话，页面上那个「恢复」按钮就废了，用户只剩「点结束、恢复、再点开始」这一条
    路——代价是把另外两条正常的链路一起停掉。

    开这个口子不破坏固化：自动停用时 `enabled` **本来就还是 True**，
    `disabled_reason` 与失败计数是调度器自己的状态、不是用户填的配置，所以这一下
    不动固化记录里的任何一个字段。
    """
    _start(console)
    console.repository.record_mission_failure(console.task_id("SCAN"), exit_code=1, limit=1)
    assert console.task("SCAN")["disabled_reason"] is not None

    response = console.patch("SCAN", {"enabled": True})

    assert response.status_code == 200, response.text
    assert console.task("SCAN")["disabled_reason"] is None


def test_reviving_while_running_may_not_smuggle_in_a_param_change(console: Console) -> None:
    """口子只给「清停用状态」，不给「趁着恢复顺手改一笔」。"""
    console.patch("PIRATE", {"params": {"radius": 5}})
    _start(console)
    console.repository.disable_mission_task(console.task_id("PIRATE"), "连续 3 次异常退出")

    response = console.patch("PIRATE", {"enabled": True, "params": {"radius": 30}})

    assert response.status_code == 409
    assert console.task("PIRATE")["params"] == {"radius": 5}
    assert console.task("PIRATE")["disabled_reason"] is not None


def test_enabling_a_chain_that_is_not_disabled_is_still_refused(console: Console) -> None:
    """没被停用的行收到 `enabled: true` 不是「恢复」，是在勾一条没参与的链路。"""
    _start(console)

    response = console.patch("BOT", {"enabled": True})

    assert response.status_code == 409


def test_the_configuration_is_editable_again_after_stopping(console: Console) -> None:
    """「只有结束状态才可以修改」的另一半：结束之后必须真的能改回来。"""
    _start(console)
    assert console.patch("PIRATE", {"params": {"radius": 9}}) is not None
    console.client.post("/api/scheduler/stop")

    response = console.patch("PIRATE", {"params": {"radius": 9}})

    assert response.status_code == 200, response.text
    assert console.task("PIRATE")["params"] == {"radius": 9}


def test_a_child_that_is_still_running_keeps_the_configuration_locked(console: Console) -> None:
    """「结束之后 runner 还在收尾」算不算停止状态：**只要手上还有子进程就不算。**

    正常路径上 `stop()` 是同步的（`terminate()` 之后 `wait()`），所以这一条永远
    不会拦住用户。这里直接把调度器的开关关掉、把子进程留在手上，钉住的是那个
    将来才会出现的窗口：哪天收尾改成异步的，锁必须自己跟着延长，而不是在收尾
    途中静默放行一次改参数。
    """
    _start(console)
    console.scheduler.tick()
    assert console.get()["current"] is not None

    console.scheduler._enabled = False  # noqa: SLF001 - 造出「关了但子进程还在」

    assert console.get()["config_locked"] is True
    refused = console.patch("PIRATE", {"params": {"radius": 7}})
    assert refused.status_code == 409


def test_a_new_bot_round_is_still_allowed_while_running(console: Console) -> None:
    """「重开一轮」不写任何一个配置字段，所以它不在这道锁里。

    它只把 `round_started_at_utc` 推到当前，也就是「按同一套配置再跑一遍」——
    固化记录里的每个字段都还是原样。挡掉它的话，用户要开新一轮就得先把整台
    调度器停下来。
    """
    _start(console)

    assert console.new_round().status_code == 200


# -- 配置固化 -------------------------------------------------------------------


def test_starting_freezes_the_configuration_of_that_moment(console: Console) -> None:
    """「开始」那一下抄一份，页面据此回答「这一轮到底按什么跑的」。"""
    # 海盗那一行种子里是不参与的，这里连同参与一起打开：记录只摆出参与调度的
    # 任务（见 `test_the_record_only_shows_the_tasks_that_take_part`）。
    console.patch("PIRATE", {"enabled": True, "params": {"radius": 6}})
    _start(console)

    frozen = console.get()["frozen_config"]
    assert isinstance(frozen, dict)
    assert frozen["frozen_at_utc"].startswith("2026-08-09T12:00")
    pirate = next(task for task in frozen["tasks"] if task["kind"] == "PIRATE")
    assert pirate["params"] == {"radius": 6}
    assert pirate["summary"] == "半径 6"
    assert frozen["changes"] == ["首次记录"]


def test_the_record_only_shows_the_tasks_that_take_part(console: Console) -> None:
    """用户口径 2026-08-17：「未生效的任务项，不应留在固化记录里」。

    这份记录在页面上回答的是「这一轮到底要跑什么」，没勾选参与的任务这一轮根本
    不会被起，混在里面只会让人分不清哪几条是真的在飞。

    断言钉的是**整张清单**而不是「不含某个名字」：只查名字的话，把过滤写成
    「漏掉某一条」照样绿。条数也一并钉住。
    """
    # 种子：海盗与 bot 不参与，扫描与军力榜参与。这里把海盗打开、扫描关掉，
    # 于是这一轮参与的恰好是海盗与军力榜——两个 kind 都不是种子里的默认状态，
    # 断言才不会被「碰巧和默认一致」蒙混过去。
    console.patch("PIRATE", {"enabled": True})
    console.patch("SCAN", {"enabled": False})
    _start(console)

    frozen = console.get()["frozen_config"]
    assert isinstance(frozen, dict)
    tasks = frozen["tasks"]
    assert isinstance(tasks, list)
    assert [task["kind"] for task in tasks] == ["PIRATE", "RANKING"]
    assert len(tasks) == 2
    assert all(task["enabled"] is True for task in tasks)


def test_a_stopped_scheduler_shows_no_frozen_configuration(console: Console) -> None:
    """停着的时候「本轮」不存在。把上一轮那份继续挂着会被读成「现在跑的就是这套」。"""
    _start(console)
    assert console.get()["frozen_config"] is not None

    console.client.post("/api/scheduler/stop")

    assert console.get()["frozen_config"] is None
    assert console.get()["config_locked"] is False


def test_the_second_start_records_what_changed_in_between(console: Console) -> None:
    """用户口径里的「记录任务内容」有两半，这是「改了什么、什么时候改的」那半。"""
    console.patch("PIRATE", {"params": {"radius": 5}})
    _start(console)
    console.client.post("/api/scheduler/stop")
    console.clock.now = NOW + timedelta(hours=1)
    console.patch("PIRATE", {"params": {"radius": 12}})
    console.patch("BOT", {"priority": -1})
    _start(console)

    frozen = console.get()["frozen_config"]
    assert isinstance(frozen, dict)
    # 次序按链路（`MissionKind` 的声明顺序）走，不按改动发生的先后：两份记录
    # 逐条对比时，同一条链路必须落在同一格。
    assert frozen["changes"] == [
        "侦查+攻击海盗：半径 5 → 12",
        "扫描+攻击 bot：优先级 1 → -1",
    ]


def test_pressing_start_twice_does_not_record_a_second_freeze(console: Console) -> None:
    """第二下什么都没变，记下来只会把真正改过的那几条淹掉。"""
    _start(console)
    _start(console)

    assert len(console.scheduler.config_freezes()) == 1


def test_the_freeze_is_written_where_a_restarted_console_can_read_it(console: Console) -> None:
    """控制台重启后内存里的一切都没了，而用户多半是重启之后才来翻这份记录。"""
    console.patch("PIRATE", {"params": {"radius": 4}})
    _start(console)

    lines = console.freeze_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    # 拿读回来的那一份断言，而不是在这行 JSON 上找子串：这条记录的用途就是
    # **被下一个进程读回来**，只断言「文件里有 radius 这几个字母」的话，一个
    # 存得下、读不回的格式照样能满足。
    reloaded = MissionFreezeLog(console.freeze_log).latest()
    assert reloaded is not None
    pirate = reloaded.task(MissionKind.PIRATE)
    assert pirate is not None
    assert json.loads(pirate.params_json) == {"radius": 4}


# -- 重开一轮 -------------------------------------------------------------------


def test_a_new_round_pushes_the_round_start_to_now(console: Console) -> None:
    """不推的话，上一轮打完的那批目标今天仍算「已完成」，新一轮永远开不起来。"""
    response = console.new_round()

    assert response.status_code == 200
    row = next(row for row in console.repository.mission_tasks() if row.kind == "BOT")
    assert row.round_started_at_utc == NOW


# -- 孤儿 -----------------------------------------------------------------------


def test_an_orphan_run_is_surfaced_with_its_pid(tmp_path: Path) -> None:
    """上次没走正常关闭路径留下的行，页面顶部要亮红条。

    pid 是给人拿去任务管理器里核对的，**不是给我们开枪用的**——pid 会被系统
    回收复用。
    """
    engine = create_database_engine(scratch_database_url(tmp_path, "orphan.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    repository = SqlAlchemyRepository(factory)
    repository.ensure_mission_rows(now_utc=NOW)
    repository.begin_mission_run(
        MissionKind.SCAN,
        task_id=next(
            row.id for row in repository.mission_tasks() if row.kind == MissionKind.SCAN.value
        ),
        command=["python"],
        pid=4321,
        started_at_utc=NOW - timedelta(hours=1),
        log_path="var/logs/mission-scan.log",
    )
    app = create_persistent_app(factory, local_token=TOKEN, tick_interval_s=3600.0)

    with TestClient(app, headers={"X-Evo-Helper-Token": TOKEN}) as client:
        assert client.get("/api/scheduler").json()["orphan_pid"] == 4321

        assert client.post("/api/scheduler/force-kill").status_code == 200

        body = client.get("/api/scheduler").json()
        assert body["orphan_pid"] is None
        assert body["running"] is False


def test_the_console_writes_its_freezes_under_var(tmp_path: Path) -> None:
    """默认的固化记录落在 `var/` 里，和子进程日志同一个地方。

    只建 app、不点「开始」，所以这条不会真的写出文件——`MissionFreezeLog` 是
    读构造、写追加的。钉住的是**接线**：控制台自己建调度器时必须给它一个带
    路径的账本，忘了给就只留在内存里，重启一次全没了，而那种丢失是静默的。
    """
    engine = create_database_engine(scratch_database_url(tmp_path, "wiring.db"))
    Base.metadata.create_all(engine)
    app = create_persistent_app(
        create_session_factory(engine), local_token=TOKEN, tick_interval_s=3600.0
    )

    assert app.state.mission_scheduler.freeze_log_path == DEFAULT_FREEZE_LOG


def test_force_kill_stops_the_child_we_do_know_about(console: Console) -> None:
    """认识的那个进程照常停掉；不认识的 pid 一律不碰。"""
    console.start()
    console.scheduler.tick()

    console.client.post("/api/scheduler/force-kill")

    body = console.get()
    assert body["current"] is None
    assert body["running"] is False


# -- 页面顶栏的契约 -------------------------------------------------------------


def test_the_header_gets_the_state_the_stopwatch_and_the_label(console: Console) -> None:
    """页面顶栏那三样——跑没跑、跑了多久、跑的是哪条链路——全在这个回包里。

    这三个字段是「服务端下发、页面照抄」：页面只做显示，一个字都不自己拼。
    所以少一个字段不会报错，只会让顶栏静静地缺一块——秒表不动、链路名空着，
    而调度器其实好好地在派舰队。这条把三样一次钉住。

    （这原本是桌面悬浮窗的契约测试。悬浮窗 2026-08-17 删掉了，但它读的正是
    页面读的同一份回包，所以断言留下来，改成对着页面的口径写。）
    """
    stopped = console.get()
    assert stopped["running"] is False
    assert stopped["current"] is None
    assert stopped["started_at_utc"] is None

    console.start()
    console.scheduler.tick()
    console.clock.now = NOW + timedelta(minutes=2)

    body = console.get()
    assert body["running"] is True
    # 顶栏那块「已运行」秒表从它算起。
    assert body["started_at_utc"].startswith("2026-08-09T12:00")
    current = body["current"]
    assert isinstance(current, dict)
    # 链路名由服务端下发，页面不自己拼——两处各写一份就会有一天对不上。
    assert current["label"] == "扫描全星系 bot"
    assert current["started_at_utc"].startswith("2026-08-09T12:00")


# -- 旧页面 ---------------------------------------------------------------------


def test_the_old_missions_page_still_renders(console: Console) -> None:
    """页面重做是下一个任务。这一步只加接口，不许把 `/missions` 弄崩。"""
    assert console.client.get("/missions").status_code == 200
    assert console.client.get("/logs").status_code == 200


def test_every_row_carries_a_revive_button(console: Console) -> None:
    """每一行都要有恢复的入口，任何一条链路都可能被自动停用。

    **行现在由页面脚本按 `/api/scheduler` 下发的任务列表建**（行数是用户加出来
    的，服务端渲染不出来），所以这里钉的是建行那一段：`buildRow` 无条件给每一行
    加上这个按钮，而不是只给某一类加。写在 `if` 里面一次，就会有链路再也恢复
    不了。显隐仍由 `status === '已停用'` 决定。
    """
    page = console.client.get("/missions").text

    assert "btn small mission-revive" in page
    # 挂在建行函数上、不在任何分支里：`revive.hidden = true` 与它紧邻，
    # 而按 kind 分叉的那几个（重开一轮、删除）在后面单独一段。
    assert page.index("mission-revive") < page.index("mission-new-round")


# -- bot 命令行 ---------------------------------------------------------------


def test_the_launched_bot_command_has_no_probe_and_no_thresholds(console: Console) -> None:
    """runner 拿到的 argv 只有目标和 `--attack`。

    ⚠️ **多传一个参数的后果不是「多余」，是这条链路起不来**：`bot_loop` 的
    argparse 见到不认识的 `--probe` / `--tier-thresholds` 会 `SystemExit(2)`，
    而调度器看到的只是退出码非零、连撞三次就把整条 bot 链路自动停用。
    分档已经整套删除（用户口径 2026-08-13）。

    这条同时守着 `--attack` 必须在：漏掉它这一轮只会站过去看一眼，不报错、
    看着一切正常，而一发都没打。
    """
    _seed_bot(console.repository, Coordinate(2, 137, 14))
    console.patch(
        "BOT",
        {"enabled": True, "params": {"galaxy": 2, "first_system": 130, "last_system": 140}},
    )
    console.patch("PIRATE", {"enabled": False})
    console.patch("SCAN", {"enabled": False})
    _start(console)
    console.scheduler.tick()

    command = next(
        item for item in console.launcher.commands if "evo_helper.tools.bot_loop" in item
    )

    assert "--attack" in command
    assert "--probe" not in command
    assert "--tier-thresholds" not in command


def test_the_tier_thresholds_api_is_gone(console: Console) -> None:
    """`/api/tier-thresholds` 与 `/tiers` 都不该再在了。

    留一条只读的旧接口比删掉更糟：页面上那三个框还能填、还能保存，而没有任何
    地方读它——用户会以为自己改了这一轮的打法。
    """
    assert console.client.get("/api/tier-thresholds").status_code == 404
    assert console.client.get("/tiers").status_code == 404


# -- 多任务：新建、删除、出发星球与航线数 ---------------------------------------
#
# 用户口径（2026-08-13）：「之后的任务需要配置一个出发星球（默认主星，也就是第一
# 颗），以及航线数。也就是可能会新增多个同一个类型的任务，比如 2 个 bot 攻击」。
# 追问确认：**只有 bot 攻击需要多任务**，海盗与扫描保持一个。


def _create(console: Console, **payload: object):  # type: ignore[no-untyped-def]
    body = {"kind": "BOT", "name": "2 号星"} | payload
    return console.client.post("/api/missions", json=body)


def test_a_second_bot_task_can_be_created(console: Console) -> None:
    """新建之后页面上就该有两行 bot。"""
    response = _create(console, fleet_lines=2)

    assert response.status_code == 201, response.text
    kinds = [item["kind"] for item in console.get()["tasks"]]  # type: ignore[union-attr]
    assert kinds.count("BOT") == 2


def test_a_new_task_starts_switched_off(console: Console) -> None:
    """**点了新建不该就开始派舰队。**

    刚建出来的任务还没填范围、也没排优先级。启用那一下会走 PATCH，
    而那条路上有参数校验。
    """
    created = _create(console).json()

    assert created["enabled"] is False


def test_a_task_without_a_name_is_named_after_the_galaxy_it_departs_from(
    console: Console,
) -> None:
    """任务名不再手输，按出发点的银河系派生：`5:261:8` → 「5系攻击」。

    ⚠️ 这条用例原先钉的是反面（名字留空 → 400，理由是「两行长得一模一样，
    用户分不出改的是哪一个」）。2026-08-22 改版把页面上那个任务名输入框撤了，
    于是「留空」从一种输入错误变成了**唯一的常规走法**——继续拒掉它，页面上的
    「新建」按钮就永远点不动。「两行长得一模一样」这件事由下一条用例接手守：
    重名自动加序号。
    """
    created = _create(console, name="   ", origin="5:261:8").json()

    assert created["label"] == "5系攻击"


def test_a_second_task_in_the_same_galaxy_gets_a_number_instead_of_the_same_name(
    console: Console,
) -> None:
    """同一个银河系下的第二个任务叫「5系攻击 2」。

    任务名是日志、运行历史、配置固化记录里认人的那个字段，重名会让事后追查
    分不开是哪一个。序号取「第一个没被占用的」，所以名字一旦落库就不再变。
    """
    first = _create(console, name=None, origin="5:261:8").json()
    second = _create(console, name=None, origin="5:250:3").json()

    assert first["label"] == "5系攻击"
    assert second["label"] == "5系攻击 2"


def test_changing_the_origin_renames_the_task_to_match(console: Console) -> None:
    """换了出发点就跟着换名字，否则页面上会出现「7系攻击」从 5 系出发。

    ⚠️ 改名只发生在**真的动了出发点**的那一次 PATCH 上；拖顺序、勾复选框都不碰
    名字（另有用例钉着「只改 priority 不动出发点」）。页面加载时也绝不批量改库。
    """
    created = _create(console, name=None, origin="5:261:8").json()

    updated = console.client.patch(
        f"/api/missions/{created['task_id']}", json={"origin": "7:228:15"}
    ).json()

    assert updated["label"] == "7系攻击"


def test_only_the_bot_chain_may_have_more_than_one_task(console: Console) -> None:
    """海盗每天 32 次是**账号级**配额，扫描恒在最后一位且永远有活干。

    给这两条链路加第二个任务不会让它们打得更多，只会让页面上多一行看着能配、
    实际互相抢同一份配额的东西。
    """
    for kind in ("PIRATE", "SCAN"):
        response = console.client.post("/api/missions", json={"kind": kind, "name": "另一个"})
        assert response.status_code == 400, kind


def test_a_new_task_defaults_to_the_home_planet_and_the_global_line_limit(
    console: Console,
) -> None:
    """留空表示**跟着全局走**，而回显必须是解析之后的值。

    显示成空白等于让用户以为舰队不知道从哪出发。
    """
    created = _create(console).json()

    assert created["origin"] == "2:137:18"
    assert created["origin_is_default"] is True
    assert created["fleet_lines_is_default"] is True


def test_a_created_task_keeps_the_planet_and_lines_it_was_given(console: Console) -> None:
    created = _create(console, origin="2:137:18", fleet_lines=2).json()

    assert created["origin"] == "2:137:18"
    assert created["fleet_lines"] == 2
    assert created["origin_is_default"] is False
    assert created["fleet_lines_is_default"] is False


def test_zero_lines_is_refused_rather_than_stored(console: Console) -> None:
    """0 条航线的任务永远派不出去，而它在页面上看起来完全正常（「等航线」）。"""
    assert _create(console, fleet_lines=0).status_code == 400

    created = _create(console).json()
    assert (
        console.client.patch(
            f"/api/missions/{created['task_id']}", json={"fleet_lines": 0}
        ).status_code
        == 400
    )


def test_a_malformed_origin_is_refused_rather_than_guessed(console: Console) -> None:
    """回落到主星的话，用户以为改成了 2 号星，实际舰队照旧从主星出发。"""
    created = _create(console).json()

    response = console.client.patch(f"/api/missions/{created['task_id']}", json={"origin": "9:250"})

    assert response.status_code == 400
    assert "9:250" in response.json()["detail"]


def test_another_planet_is_accepted_now_that_the_helper_can_switch_to_it(
    console: Console,
) -> None:
    """**别的星球现在收得下了。**

    这条原先钉的是反面（400，理由是助手还不会切星球）。那道临时闸门随
    「切换星球」实装一起删了：runner 开工时真的把当前星球切过去，切不成就一发
    都不派。页面上再拦一道等于把这个功能关在门外。

    仍然要拦的只有「写不成坐标」那一档，见上一条用例。
    """
    created = _create(console).json()

    response = console.client.patch(
        f"/api/missions/{created['task_id']}", json={"origin": "9:250:8"}
    )

    assert response.status_code == 200
    assert response.json()["origin"] == "9:250:8"


def test_clearing_the_origin_puts_it_back_on_the_home_planet(console: Console) -> None:
    """空串是一个**动作**：退回「用全局主星」。

    它和 `null`（这次不动它）必须分得开，否则任何一次只改优先级的 PATCH 都会
    顺手把出发星球抹掉。
    """
    created = _create(console, origin="2:137:18").json()
    assert created["origin_is_default"] is False

    updated = console.client.patch(
        f"/api/missions/{created['task_id']}", json={"origin": ""}
    ).json()

    assert updated["origin_is_default"] is True
    assert updated["origin"] == "2:137:18"


def test_changing_only_the_priority_never_touches_the_origin(console: Console) -> None:
    """**不许改用户已配置的取值。** 拖一下顺序不该把出发星球一起清掉。"""
    created = _create(console, origin="2:137:18", fleet_lines=2).json()

    updated = console.client.patch(
        f"/api/missions/{created['task_id']}", json={"priority": 3}
    ).json()

    assert updated["origin_is_default"] is False
    assert updated["fleet_lines"] == 2


def test_the_last_task_of_a_chain_cannot_be_deleted(console: Console) -> None:
    """删光了页面上就再也建不回来（海盗与扫描连「新建」的入口都没有）。

    不想让它跑的正确做法是取消勾选。
    """
    response = console.client.delete(f"/api/missions/{console.task_id('PIRATE')}")

    assert response.status_code == 400


def test_a_second_bot_task_can_be_deleted_again(console: Console) -> None:
    created = _create(console).json()

    assert console.client.delete(f"/api/missions/{created['task_id']}").status_code == 204
    assert [item["kind"] for item in console.get()["tasks"]].count("BOT") == 1  # type: ignore[union-attr]


def test_tasks_cannot_be_created_or_deleted_while_the_scheduler_runs(console: Console) -> None:
    """运行中配置已固化。加一行、删一行同样是改配置。"""
    created = _create(console).json()
    _start(console)

    assert _create(console, name="第三个").status_code == 409
    assert console.client.delete(f"/api/missions/{created['task_id']}").status_code == 409


def test_two_bot_tasks_keep_their_own_priorities(console: Console) -> None:
    """**拖拽排序按 id 寻址。**

    按 kind 寻址的话，拖动其中一行会打到不确定的那一行上——用户配好的优先级
    就此变成随机的。
    """
    created = _create(console).json()
    main = console.task_id("BOT")

    console.client.patch(f"/api/missions/{main}", json={"priority": 0})
    console.client.patch(f"/api/missions/{created['task_id']}", json={"priority": 1})

    rows = {
        item["task_id"]: item["priority"]
        for item in console.get()["tasks"]  # type: ignore[union-attr]
    }
    assert rows[main] == 0
    assert rows[created["task_id"]] == 1


def test_the_row_summary_says_which_planet_and_how_many_lines(console: Console) -> None:
    """多任务之后，「这一行从哪出发、能占几条」是区分两行 bot 的第一件事，
    而它俩都不在参数框里。
    """
    created = _create(console, fleet_lines=2).json()

    assert "2:137:18" in created["summary"]
    assert "2 条航线" in created["summary"]


def test_a_military_bot_plan_uses_global_tiers_and_selected_planets(console: Console) -> None:
    """任务只保存军力范围与星球选择；档位统一落在攻击配置页。"""
    task_id = console.task_id("BOT")
    params = {
        "by_military": True,
        "top_n": 50,
        "max_score": 100_000,
        "rescan_after_hours": 6,
    }
    first = console.client.post(
        "/api/attack-planets", json={"galaxy": 2, "system": 137, "position": 18}
    )
    second = console.client.post(
        "/api/attack-planets", json={"galaxy": 9, "system": 250, "position": 8}
    )
    config = console.client.put(
        "/api/attack-config",
        json={
            "tiers": [
                {"min_score": 20_000, "preset": "CCC"},
                {"min_score": 5_000, "preset": "BBB"},
                {"min_score": 0, "preset": "AAA"},
            ]
        },
    )

    patched = console.client.patch(f"/api/missions/{task_id}", json={"params": params})
    origins = console.client.put(
        f"/api/missions/{task_id}/origins",
        json=[
            {"planet_id": first.json()["planet_id"], "fleet_lines": 4, "enabled": True},
            {"planet_id": second.json()["planet_id"], "fleet_lines": 2, "enabled": True},
        ],
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert config.status_code == 200, config.text
    assert patched.status_code == 200, patched.text
    assert patched.json()["params"] == params
    assert origins.status_code == 200, origins.text
    assert origins.json() == [
        {
            "planet_id": first.json()["planet_id"],
            "galaxy": 2,
            "system": 137,
            "position": 18,
            "fleet_lines": 4,
            "enabled": True,
        },
        {
            "planet_id": second.json()["planet_id"],
            "galaxy": 9,
            "system": 250,
            "position": 8,
            "fleet_lines": 2,
            "enabled": True,
        },
    ]
    assert config.json()["tiers"][0]["preset"] == "CCC"


def _military_bot(console: Console, *coordinates: tuple[Coordinate, int]) -> int:
    """把种子那行 bot 改成军力优先，并配上多出发点。返回它的 id。"""
    task_id = console.task_id("BOT")
    console.client.patch(
        f"/api/missions/{task_id}", json={"params": {"by_military": True, "top_n": 50}}
    )
    payload = []
    for coordinate, lines in coordinates:
        planet = console.client.post(
            "/api/attack-planets",
            json={
                "galaxy": coordinate.galaxy,
                "system": coordinate.system,
                "position": coordinate.position,
            },
        )
        assert planet.status_code == 201, planet.text
        payload.append(
            {"planet_id": planet.json()["planet_id"], "fleet_lines": lines, "enabled": True}
        )
    saved = console.client.put(f"/api/missions/{task_id}/origins", json=payload)
    assert saved.status_code == 200, saved.text
    return task_id


def test_a_military_row_shows_its_real_origins_not_the_stale_task_level_count(
    console: Console,
) -> None:
    """⚠️ **行头那句话必须念 `mission_task_origins`，不是任务级那个残值。**

    生产实证（2026-08-18）：用户配的是 `4:277:15=6 线` + `9:250:8=2 线`，页面上却
    写着「4:277:15 · 7 条航线」——那个 7 是 `mission_tasks.fleet_lines`，
    一个加多出发点之前留下的、`replace_mission_origins` 从不回写的残值。

    顺带钉住「已配 X 条 / 上限 Y 条」那句提示：用户口径（2026-08-18）
    「我配置时已经手动确认了不会超过总航线数」——他自己确认，那就得有东西给他看。
    """
    console.client.put(
        "/api/attack-config",
        json={"tiers": [{"min_score": 0, "preset": "AAA"}], "account_line_limit": 9},
    )
    task_id = _military_bot(console, (Coordinate(4, 277, 15), 6), (Coordinate(9, 250, 8), 2))
    console.client.patch(f"/api/missions/{task_id}", json={"enabled": True})

    summary = str(console.task("BOT")["summary"])

    assert "4:277:15 · 6 条" in summary
    assert "9:250:8 · 2 条" in summary
    assert "7 条航线" not in summary, "任务级那个残值一个字都不该出现在军力行上"
    assert "全账号已配 8 条 / 上限 9 条" in summary


def test_the_row_says_no_account_limit_is_set_rather_than_inventing_one(
    console: Console,
) -> None:
    """⚠️ **没填账号上限时页面不许编一个数出来。**

    用户口径（2026-08-18）：「账号的默认权限不应在代码中进行配置……实际通过科技
    升级，使用道具，人为占用，都会影响到留给你的航线数量」。所以那一档的实话是
    「已配 8 条 · 未设账号上限」。

    写成「/ 上限 9 条」的话，用户会照着那个 9 去配他的星球——而那个 9 是程序编的，
    没有任何人知道它对不对。这条同时守住「留空不是 0」：显示成「上限 0 条」
    或者「已超出」都会让它转红。
    """
    task_id = _military_bot(console, (Coordinate(4, 277, 15), 6), (Coordinate(9, 250, 8), 2))
    console.client.patch(f"/api/missions/{task_id}", json={"enabled": True})

    summary = str(console.task("BOT")["summary"])

    assert "全账号已配 8 条 · 未设账号上限" in summary
    assert "上限 9" not in summary, "没人说过 9，页面不许自己编一个"
    assert "已超出" not in summary


def test_the_row_says_so_when_the_configured_lines_exceed_the_account_limit(
    console: Console,
) -> None:
    """配超了要说出来——但**不拦**。

    用户可能正编辑到一半，也可能先配好再去开加成道具（常态 6、开道具 9）。
    所以保存照样 200，只是行头上多一句「已超出」。硬拦的实现会让这条转红。
    """
    console.client.put(
        "/api/attack-config",
        json={"tiers": [{"min_score": 0, "preset": "AAA"}], "account_line_limit": 6},
    )
    task_id = _military_bot(console, (Coordinate(4, 277, 15), 6), (Coordinate(9, 250, 8), 2))
    enabled = console.client.patch(f"/api/missions/{task_id}", json={"enabled": True})

    assert enabled.status_code == 200, enabled.text
    summary = str(console.task("BOT")["summary"])
    assert "全账号已配 8 条 / 上限 6 条（已超出）" in summary


def test_saving_the_origins_never_writes_back_the_task_level_fleet_lines(
    console: Console,
) -> None:
    """⚠️ **`replace_mission_origins` 不许同步任务级那一列。**

    回写等于造出两份真相：多出发点那张表一份、任务级那一列一份，而两份迟早对不上
    ——它们对不上正是这次事故的形状。真相只有一份，显示层去读那一份就是了。
    """
    before = int(console.task("BOT")["fleet_lines"])  # type: ignore[arg-type]
    _military_bot(console, (Coordinate(4, 277, 15), 6), (Coordinate(9, 250, 8), 2))

    row = console.repository.mission_task(console.task_id("BOT"))
    assert row is not None
    assert row.fleet_lines is None or row.fleet_lines == before


def test_changing_a_planets_lines_shows_up_in_the_change_list(console: Console) -> None:
    """「把 2 号星的航线从 2 改成 3」必须出现在「改动」列里。

    ⚠️ 没有这一段的后果是**静默丢账**：军力攻击的真相全在 `origins` 里，而逐条
    对比原先只比 `origin` / `fleet_lines`（任务级残值，永远不变），于是这一次改动
    在记录里一个字都不留，看起来像是什么都没改。
    """
    task_id = _military_bot(console, (Coordinate(4, 277, 15), 6), (Coordinate(9, 250, 8), 2))
    # 记录只摆出参与调度的任务，所以这一行得先勾上。
    console.client.patch(f"/api/missions/{task_id}", json={"enabled": True})
    _start(console)
    console.client.post("/api/scheduler/stop")

    planets = console.client.get("/api/attack-planets").json()
    ids = {
        (item["galaxy"], item["system"], item["position"]): item["planet_id"] for item in planets
    }
    console.clock.now = NOW + timedelta(hours=1)
    console.client.put(
        f"/api/missions/{task_id}/origins",
        json=[
            {"planet_id": ids[(4, 277, 15)], "fleet_lines": 6, "enabled": True},
            {"planet_id": ids[(9, 250, 8)], "fleet_lines": 3, "enabled": False},
        ],
    )
    _start(console)

    frozen = console.get()["frozen_config"]
    assert isinstance(frozen, dict)
    changes = frozen["changes"]
    assert any("9:250:8 航线数 2 → 3" in item for item in changes), changes
    assert any("9:250:8 参与派遣 是 → 否" in item for item in changes), changes


def test_the_report_scan_floor_round_trips_through_the_attack_config(console: Console) -> None:
    """攻击配置页那个「翻信箱时长」存得住、读得回，非法值当场 422。

    整份替换是这一页的既有约定，所以每次 `PUT` 都要把三项一起送——这条同时钉住
    「送了时长没把档位冲掉」。
    """
    saved = console.client.put(
        "/api/attack-config",
        json={
            "tiers": [{"min_score": 0, "preset": "AAA"}],
            "blind_scrolls": 30,
            "report_scan_hours": 2,
        },
    )
    read_back = console.client.get("/api/attack-config")
    # 0 = 下界就是此刻 = 一封都翻不到，只可能是手滑。
    zero = console.client.put("/api/attack-config", json={"tiers": [], "report_scan_hours": 0})
    negative = console.client.put("/api/attack-config", json={"tiers": [], "report_scan_hours": -3})
    words = console.client.put(
        "/api/attack-config", json={"tiers": [], "report_scan_hours": "两小时"}
    )

    assert saved.status_code == 200, saved.text
    assert read_back.json()["report_scan_hours"] == 2
    assert read_back.json()["blind_scrolls"] == 30
    assert read_back.json()["tiers"] == [{"min_score": 0.0, "preset": "AAA"}]
    # 两道关各管一段：数字但不合理的由调度器那把尺子拒（400，带中文原因），
    # 压根不是整数的在 pydantic 那层就进不来（422）。
    assert zero.status_code == 400, zero.text
    assert "别填 0" in zero.json()["detail"], "拒了还得说清「留空」和「填 0」不是一回事"
    assert negative.status_code == 400, negative.text
    assert words.status_code == 422, words.text


def test_a_blank_report_scan_floor_is_stored_as_blank_not_as_a_number(
    console: Console,
) -> None:
    """留空要一路留空到库里：回落成具体数字的话，日后调默认值它不跟。"""
    console.client.put("/api/attack-config", json={"tiers": [], "report_scan_hours": 6})

    cleared = console.client.put("/api/attack-config", json={"tiers": []})

    assert cleared.status_code == 200, cleared.text
    assert console.client.get("/api/attack-config").json()["report_scan_hours"] is None


# -- 调度节奏的三个旋钮 --------------------------------------------------------
#
# 判据与日志在 `tests/integration/application/test_behaviour_knobs.py`。
# 这里钉的是**接线**：`PUT /api/attack-config` 收得下、`GET` 原样回读、
# 不可能的取值被拒，而且改一项不会把另一项冲掉。


def test_the_pacing_knobs_default_to_blank_meaning_follow_the_code(console: Console) -> None:
    """一份没配过的库里，三个框都是空的——也就是「跟着代码里的默认值走」。

    ⚠️ 空必须是 `None` 而不是 0：`0` 在翻信箱冷却上是一个**合法且不同**的取值
    （每一轮开工都翻）。两者混在一起，升级当天所有老库都会静悄悄换个行为。
    """
    body = console.client.get("/api/attack-config").json()

    assert body["unknown_line_hold_minutes"] is None
    assert body["reconcile_cooldown_minutes"] is None
    assert body["bot_revisit_hours"] is None


def test_the_pacing_knobs_round_trip_through_the_attack_config(console: Console) -> None:
    saved = console.client.put(
        "/api/attack-config",
        json={
            "tiers": [{"min_score": 0, "preset": "AAA"}],
            "unknown_line_hold_minutes": 45,
            "reconcile_cooldown_minutes": 0,
            "bot_revisit_hours": 6,
        },
    )

    assert saved.status_code == 200, saved.text
    read_back = console.client.get("/api/attack-config").json()
    assert read_back["unknown_line_hold_minutes"] == 45
    # 0 必须原样回来，不能被读成「留空」。
    assert read_back["reconcile_cooldown_minutes"] == 0
    assert read_back["bot_revisit_hours"] == 6
    assert read_back["tiers"] == [{"min_score": 0.0, "preset": "AAA"}]


def test_saving_the_page_clears_a_knob_that_was_left_blank(console: Console) -> None:
    """这一页是**整份替换**：留空送上来就是「改回跟着默认值走」。

    这条守的是留空那一侧真的写得回去——只认「有值才写」的实现会让用户清不掉
    一个填错的数，页面上删掉它、保存、刷新，那个数又回来了。
    """
    console.client.put(
        "/api/attack-config",
        json={"tiers": [], "unknown_line_hold_minutes": 45, "bot_revisit_hours": 6},
    )
    console.client.put("/api/attack-config", json={"tiers": [], "bot_revisit_hours": 6})

    body = console.client.get("/api/attack-config").json()
    assert body["unknown_line_hold_minutes"] is None
    assert body["bot_revisit_hours"] == 6


@pytest.mark.parametrize(
    ("field", "value"),
    [
        # 0 = 「读不到飞行时间就当没占航线」，被实机推翻掉的旧口径。
        ("unknown_line_hold_minutes", 0),
        # 够到「放弃等战报」那条线就会锁死航线。
        ("unknown_line_hold_minutes", 360),
        ("reconcile_cooldown_minutes", -1),
        # 上界 = 战报宽限期（默认 30）的一半。
        ("reconcile_cooldown_minutes", 16),
        # 0 = 取消排除，而候选池按军力降序排。
        ("bot_revisit_hours", 0),
        ("bot_revisit_hours", 169),
        # 0 = 取消排除，而那正是「面板名读不出」这条功能要修的缺陷本身。
        ("unreadable_exclusion_hours", 0),
        ("unreadable_exclusion_hours", 25),
    ],
)
def test_impossible_pacing_knobs_are_refused_by_the_api(
    console: Console, field: str, value: int
) -> None:
    """页面用的是**调度器那把尺子**（`validate_*`），和实机启动时同一段代码。
    两边分家的结果是「页面收下了、跑起来不是那个数」。
    """
    response = console.client.put("/api/attack-config", json={"tiers": [], field: value})

    assert response.status_code == 400, response.text
    assert console.client.get("/api/attack-config").json()[field] is None


def test_the_blind_scroll_rows_round_trip_through_the_attack_config(console: Console) -> None:
    """盲滚行数存得住、读得回，且 `0` 和 `700` 都是合法取值。

    ⚠️ `0` 那一条是这里的要害：它的意思是「一格都不拨」，和「留空 = 自动标定
    （样本不够时 700 行）」是**两个不同的取值**。哪一层把 0 读成留空，用户就再也
    配不出「不盲滚」。
    """
    zero = console.client.put("/api/attack-config", json={"tiers": [], "blind_scroll_rows": 0})
    zero_read = console.client.get("/api/attack-config").json()
    saved = console.client.put(
        "/api/attack-config",
        json={
            "tiers": [{"min_score": 0, "preset": "AAA"}],
            "blind_scrolls": 30,
            "blind_scroll_rows": 700,
        },
    )
    read_back = console.client.get("/api/attack-config").json()

    assert zero.status_code == 200, zero.text
    assert zero_read["blind_scroll_rows"] == 0, "0 被读成了留空"
    assert saved.status_code == 200, saved.text
    assert read_back["blind_scroll_rows"] == 700
    # 屏口径那一列刻意保留（它是回滚杠杆的用户入口），所以这一次 PUT 同时送
    # 两项，钉住「送了行数没把屏数冲掉」。
    assert read_back["blind_scrolls"] == 30
    assert read_back["tiers"] == [{"min_score": 0.0, "preset": "AAA"}]


def test_a_blank_blind_scroll_rows_stays_blank(console: Console) -> None:
    """留空要一路留空到库里：回落成 700 的话，日后调默认值它不跟，自动标定也再没机会说话。"""
    console.client.put("/api/attack-config", json={"tiers": [], "blind_scroll_rows": 300})

    cleared = console.client.put("/api/attack-config", json={"tiers": []})

    assert cleared.status_code == 200, cleared.text
    assert console.client.get("/api/attack-config").json()["blind_scroll_rows"] is None


def test_a_negative_blind_scroll_rows_is_refused_by_the_schedulers_ruler(
    console: Console,
) -> None:
    """负数由**调度器那把尺子**拒（400，带中文原因），不是 pydantic。

    两道关各管一段，和 `report_scan_hours` 那一对完全同形：压根不是整数的在
    schemas 那层就进不来（422），数字但不合理的必须让调度器判——页面自己再判一遍
    的结果是「页面收下了、实机跑起来不是那个数」。
    """
    response = console.client.put("/api/attack-config", json={"tiers": [], "blind_scroll_rows": -1})

    assert response.status_code == 400, response.text
    assert "盲滚行数" in response.json()["detail"], "拒了还得说清拒的是哪一格"
    assert console.client.get("/api/attack-config").json()["blind_scroll_rows"] is None


def test_a_very_large_blind_scroll_rows_is_not_refused(console: Console) -> None:
    """**一个上界都不设**（用户口径 2026-08-22：盲滚行数由用户定）。

    ⚠️ 这条守的是「别拿 `FIRST_BOT_RANK`(587) 当边界拦」：那个「bot 起点」是玩家
    改名伪装出来的，不是安全边界。而这里拦一下的代价格外高——`MissionParamError`
    的后果是自动停用到用户手动恢复为止，一次「好心拦一下」能把整夜采集关掉。
    """
    saved = console.client.put("/api/attack-config", json={"tiers": [], "blind_scroll_rows": 5000})

    assert saved.status_code == 200, saved.text
    assert console.client.get("/api/attack-config").json()["blind_scroll_rows"] == 5000


@pytest.mark.parametrize("value", [3.5, "很多", True])
def test_a_blind_scroll_rows_that_is_not_an_integer_is_refused(
    console: Console, value: object
) -> None:
    """压根不是整数的在 pydantic 那层就进不来（422）。

    ⚠️ `True` 单独列一条：`bool` 是 `int` 的子类，放过去就会被存成 1 行——
    「拨一格」和「用户想表达的意思」没有任何关系。
    """
    response = console.client.put(
        "/api/attack-config", json={"tiers": [], "blind_scroll_rows": value}
    )

    assert response.status_code == 422, response.text
    assert console.client.get("/api/attack-config").json()["blind_scroll_rows"] is None


#: 攻击配置页上**全部**可空旋钮，以及一组各自合法、且两两不同的取值。
#:
#: ⚠️ **新增旋钮时必须往这里加一行。** 这张表是下面那条用例的全部输入，
#: 而那条用例是这一页唯一一条「所有旋钮一起送、一起读回」的断言。
#:
#: ⚠️ **删旋钮时也要从这里删一行。** 2026-08-18 删掉了 `military_time_pool`
#: ——它是「按读数时间取前 N 个」那个错误设计的产物（理由在
#: `domain.target_order` 模块头第 3 步）。留在这张表里的话，这条用例会为一个
#: 已经不存在的字段红掉，而红的原因看起来像是管线漏了一边。
_ALL_KNOBS = {
    "blind_scrolls": 30,
    # 盲滚行数（滚轮口径）。和上面那个屏数（慢拖口径）**同时存在**：置空它就退回
    # 慢拖，是这次改动的一键回滚，所以两列必须各自存得住。
    "blind_scroll_rows": 700,
    "report_scan_hours": 2,
    "unknown_line_hold_minutes": 45,
    "reconcile_cooldown_minutes": 0,
    "bot_revisit_hours": 6,
    "protection_exclusion_hours": 4,
    "unreadable_exclusion_hours": 3,
    "account_line_limit": 6,
    "auto_toggle_log_seconds": 90,
}


def test_every_knob_survives_a_single_save(console: Console) -> None:
    """一次 `PUT` 把所有旋钮一起送上去，**每一个**都要原样读得回来。

    ⚠️ **这条守的是合并事故，不是业务逻辑。** 这一页的六文件管线
    （models → repository → service → schemas → app → settings.html）被好几个
    并行的 PR 各加各的字段，行贴着行；解冲突时漏掉一边的典型症状是**那个字段
    静默失效**——代码照常编译，而只测自己那几项的用例照样全绿，因为漏掉的字段
    连同它自己的用例一起没了。

    所以判据必须是「一次保存之后，`_ALL_KNOBS` 里每一项都还在」，而不是各测各的。
    """
    saved = console.client.put(
        "/api/attack-config",
        json={"tiers": [{"min_score": 0, "preset": "AAA"}], **_ALL_KNOBS},
    )

    assert saved.status_code == 200, saved.text
    body = console.client.get("/api/attack-config").json()
    # 逐项比而不是整个字典比：漏了哪一个要一眼看得出来。
    for field, value in _ALL_KNOBS.items():
        assert body[field] == value, f"{field} 没能原样存下来（读回 {body[field]!r}）"
    assert body["tiers"] == [{"min_score": 0.0, "preset": "AAA"}]


def test_the_settings_page_renders_every_knob(console: Console) -> None:
    """攻击配置页真的渲染得出来，而且每个旋钮都有那个 `id`。

    ⚠️ **这一页只有这一条覆盖。** 六文件管线的另一头是 `settings.html`：模板里
    少一个上下文键、或者 `id` 和 JS 里那张表对不上，接口用例一条都不会红——它们
    根本不渲染页面，而用户只会看到一个存不进去的框。

    默认值与上界也一起断言：它们从常量传进模板（不在 HTML 里手抄），
    抄一遍之后调常量页面不跟，而页面上那几句话正是用户判断「填多少」的唯一依据。
    """
    response = console.client.get("/settings")

    assert response.status_code == 200, response.text
    body = response.text
    for knob_id in (
        "blind-scrolls",
        "blind-scroll-rows",
        "report-scan-hours",
        "line-hold",
        "reconcile-cooldown",
        "bot-revisit",
        "protection-exclusion",
        "unreadable-exclusion",
        "account-line-limit",
        "auto-toggle-log",
    ):
        assert f'id="{knob_id}"' in body, f"{knob_id} 那个框没渲染出来"
        assert f"getElementById('{knob_id}')" in body, f"{knob_id} 没接进保存那张表"
    # ⚠️ 断言的是**带标签的那一句**，不是光秃秃的 `留空 = 6`——后者和「翻信箱
    # 往回读几小时」那一格的默认值撞号，撞上了这条就什么都没验。
    assert "面板名读不出排除时长（小时，默认 6）" in body, "默认值没从常量传进模板"
    assert "上限 24 小时——超过一天" in body, "上界没从常量传进模板"
    # 盲滚那一节的行↔秒换算：三个标定常量都得真的渲染进去，那一行是用户判断
    # 「填这么多要滚多久」的唯一依据。换算本身在 JS 里，所以这里只钉住
    # 「常量到了页面上」和「那个显示位存在」。
    assert "默认的 700 行" in body, "盲滚默认行数没从常量传进模板"
    assert "减 83 行余量" in body, "盲滚自动标定的余量没从常量传进模板"
    assert "1.08 行/格" in body, "每格行数没从常量传进模板"
    assert "惯性滑行约 2.5 秒" in body, "滑行等待没从常量传进模板"
    assert 'id="blind-rows-seconds"' in body, "行↔秒换算那一行没有显示位"
    # 两个框并排放着，页面必须说清哪一个当真：屏数那一格现在完全不生效，
    # 不写明白的话用户会以为自己填的屏数还管事，而实机走的是行数那一路。
    assert "完全不上命令行" in body, "没说清屏数那个框已经不生效"
    # ⚠️ 读回必须是 `??` 而不是 `||`：0 是合法取值（「一格都不拨」），`||` 会把它
    # 显示成空框，也就是显示成「用默认值」——用户下一次保存就把 0 改掉了，而他
    # 从头到尾没看见自己填的那个 0。这一条是白盒断言，因为这一页没有 JS 测试台，
    # 而这个错误在页面上长得和「本来就没配」一模一样。
    assert "config.blind_scroll_rows ?? ''" in body, "0 会被 `||` 显示成留空"


def test_every_knob_is_blank_on_a_fresh_database(console: Console) -> None:
    """没配过的库里**每一个**旋钮都是空的 = 跟着代码里的默认值走。

    同上：升级完成那一刻行为不变，靠的就是这些列全是 NULL。任何一个被写上了
    `server_default`，这里就会读到一个具体数字，而那意味着日后调默认值它不跟。
    """
    body = console.client.get("/api/attack-config").json()

    for field in _ALL_KNOBS:
        assert body[field] is None, f"{field} 在新库里不该有值（读到 {body[field]!r}）"
