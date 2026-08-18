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

from evo_helper.application.mission_freeze import MissionFreezeLog
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.application.mission_supervisor import MissionSupervisor
from evo_helper.domain.scheduler import MissionKind, TaskStatus
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app
from support.database import scratch_database_url

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
    engine = create_database_engine(scratch_database_url(tmp_path, "console.db"))
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


def test_the_missions_controls_stay_compact_and_keep_military_origins_in_dispatch(
    client: TestClient,
) -> None:
    """顶部说明不能再挤掉任务表，军力开关与多出发点也不能回流到参数列。"""
    body = _page_body(client.get("/missions").text)

    assert 'class="missions-control-grid' in body
    assert 'class="tips"' in body
    assert "taskCell.append(modeLabel)" in body
    assert "militaryDispatch.className = 'military-dispatch'" in body
    assert "params.append(settings)" in body
    assert "editor.append(settings, originHead" not in body


def test_the_scan_row_is_not_draggable_and_says_why(client: TestClient) -> None:
    """扫描恒在最后一位，页面上就不能给它一个能拖的把手。

    它永远有活干，排在谁前面谁就永远轮不到——拖到海盗之前等于当天 32 次
    配额悄无声息地全流失。后端会拒（带 priority 的 PATCH 返回 400），
    但用户不该拖完了才发现。

    **行现在由页面脚本按 `/api/scheduler` 下发的任务列表建**（同一 kind 可以有
    多行，服务端渲染不出固定几行），所以断言落在建行那一段的判据上：
    `draggable` 取值只由「是不是 SCAN」决定，而且拖动那一路还要再挡一次
    「把别人拖到它后面」。
    """
    body = _page_body(client.get("/missions").text)

    # 建行时：SCAN 不可拖，别的都可拖。写成常量 'false' 就等于全都不能拖。
    assert "row.setAttribute('draggable', isScan ? 'false' : 'true')" in body
    # 拖动过程中也不许把别人插到它后面。
    assert "if (row.getAttribute('draggable') !== 'true') return;" in body
    assert "始终填空隙" in body


def test_every_status_survives_the_trip_to_the_page(client: TestClient) -> None:
    """每一档一个都不能合并。

    没勾的任务显示「待命」是谎话（它永远不会被起起来）；冷却中显示「等航线」
    会让用户去调航线数、调完还是不动；定时窗口那两档显示成「待命」，用户会一直
    等下一轮，而下一轮永远不来。页面按状态上色，所以每一档都得在色调表里各占
    一格——少一格就意味着有两档被当成了同一件事。
    """
    html = client.get("/missions").text

    for status in TaskStatus:
        assert status.value in html, status.name


def test_the_page_offers_a_schedule_window_labelled_in_utc_plus_eight(
    client: TestClient,
) -> None:
    """定时开关那两个输入框，以及它们头上那个写死的时区。

    时区必须写在控件旁边：用户填进去的那串数字按哪个时区解释，不写出来只能靠猜，
    猜错正好差 8 小时。（「战报补录」那个日期控件标的是 UTC，是特例。）
    """
    body = _page_body(client.get("/missions").text)

    assert "定时开关（UTC+8）" in body
    assert "'mission-enabled-from'" in body
    assert "'mission-enabled-until'" in body
    # 送上去的必须带偏移量，不带的话服务端会 400（而那是有意的）。
    assert "+08:00" in body


def test_the_page_says_the_window_does_not_cut_off_a_running_round(
    client: TestClient,
) -> None:
    """「到点不抢停」必须写在页面上。

    用户看到关闭时刻已过而任务还在跑，不写清楚就只能理解成「定时没生效」，
    然后去点强制结束——而那一下会把另外几条正常的链路一起停掉。
    """
    body = _page_body(client.get("/missions").text)

    assert "正在跑的不打断" in body or "不打断正在跑的那一轮" in body


# -- 压行高：通用说明进 ⓘ、定时开关折叠 --------------------------------------
#
# 一屏能看几行任务，取决于**最高的那一列**。原先最高的两样东西都不是这一行自己的
# 事实，而是每一行逐字重复的通用说明，以及两个绝大多数任务根本没填的日期时间框。
# 这一批用例守的是「压高度」和「别把信息弄丢」这两件事**同时**成立：
#
# - 通用说明搬进 `title`，但**必须真的进了 title**（`makeTips` 那一句），
#   不许只从页面上删掉；
# - 摘要压成一行、超出截掉，但**必须同时挂上 title**——截断了又读不到全文，
#   等于把「全账号已配 N 条 · 已超出」藏起来，而那是超配唯一的显形处；
# - 定时那一列默认折叠，但**配了定时的任务不许被折起来**。


def test_the_military_selection_criteria_moves_into_a_tooltip_instead_of_vanishing(
    client: TestClient,
) -> None:
    """那段近 200 字的「选靶：①…④…」原先每一行任务都铺一遍，是行高的大头。

    它是**通用说明**（在每一行里逐字相同），所以搬进 ⓘ。但「搬走」和「删掉」在
    页面上看起来一模一样，而这段话里有好几句是判据的一部分（「窗口门限」不决定
    打谁、有效期是划一条线而不是取最新的几个）——丢了它，页面和代码就开始分家。
    所以这里钉三件事：文字还在、进的是 `title`、不再每行铺一个 `<div>`。
    """
    body = _page_body(client.get("/missions").text)

    # ① 文字一个字都没少（抽查最容易被「精简」掉的那两句）。
    assert "选靶：① 剔除近 24 小时打过的" in body
    assert "「窗口门限」不决定打谁" in body
    # ② 它被交给 `makeTips`——也就是挂在 `title` 上，而不是又变成一行正文。
    assert "makeTips('军力选靶口径说明', MILITARY_SELECTION_TIP)" in body
    # ③ `makeTips` 真的把正文写进 `title`。少了这一句，上面两条照样绿，
    #    而页面上那个 ⓘ 悬停出来是空的——说明就等于丢了。
    assert "tips.title = text" in body
    # ④ 不再每行铺一个 `<div>`：那正是要省掉的行高。
    assert "advice.textContent" not in body


def test_the_multi_origin_note_moves_into_a_tooltip_instead_of_vanishing(
    client: TestClient,
) -> None:
    """「每颗出发星球填自己的航线；攻击档位在『攻击配置』页统一维护。」同上。

    它讲的是这个功能怎么用（每一行长得一模一样），不是这一行的事实，所以进 ⓘ。
    但它是用户找到「档位到底在哪儿改」的唯一线索，删掉就等于把那条路藏了。
    """
    body = _page_body(client.get("/missions").text)

    assert "每颗出发星球填自己的航线；攻击档位在" in body
    assert "页统一维护。" in body
    assert "makeTips('多出发点说明', MILITARY_ORIGIN_TIP)" in body
    assert "tips.title = text" in body
    # 原先那个每行一份的 `<div>` 没了。
    assert "note.textContent = '每颗出发星球" not in body


def test_the_row_summary_is_clipped_to_one_line_but_never_truncated_away(
    client: TestClient,
) -> None:
    """摘要讲的是**这一行自己的事实**，所以留在页面上，只是压成一行。

    ⚠️ **省略号和 `title` 是一对。** 摘要里「全账号已配 N 条 · 未设账号上限」
    是用户唯一能看见「航线有没有超配」的地方（那句话由
    `web.persistent_service.LineBudget.hint` 生成，`tests/integration/api/
    test_scheduler_api.py` 钉着它的内容）。压成一行之后它可能被截在屏幕外——
    那时能不能读到，全靠 `title`。只截不留 `title`，等于把超配藏了起来。
    """
    body = _page_body(client.get("/missions").text)

    # 一行不折行 + 超出用省略号。
    assert "summary.style.whiteSpace = 'nowrap'" in body
    assert "summary.style.textOverflow = 'ellipsis'" in body
    # 完整内容进 `title`，和摆出来的正文同源（都是后端下发的 `task.summary`）。
    assert "summaryLine.textContent = task.summary || ''" in body
    assert "summaryLine.title = task.summary || ''" in body


def test_a_task_that_has_a_schedule_window_is_never_folded_out_of_sight(
    client: TestClient,
) -> None:
    """⚠️ **这一条是这次改动最危险的地方。**

    定时那一列默认折叠（绝大多数任务没配定时，那三行高度买到的是两个空框），
    但把一份**已经生效**的定时藏起来，比多占几行危险得多：用户看不见它，就会
    以为定时没生效，然后去改别的东西——而任务其实每天到点就自己关掉。

    所以守两道，缺一道都算漏：
    ① 配了定时的行**默认展开**；判据是**库里有没有值**（`enabled_from_utc` /
       `enabled_until_utc`），不是页面上那两个框里此刻是什么——用框里的值来判，
       在刚建出来还没 prime 的行上会得出「没配」。
    ② 折叠那一行本身把配着的时刻念出来，所以哪怕用户自己把它折回去，
       「这个任务配了定时」照样一眼看得见。
    """
    body = _page_body(client.get("/missions").text)

    # ① 判据来自库，不是来自输入框。
    assert "const scheduled = Boolean(task.enabled_from_utc || task.enabled_until_utc);" in body
    # ② 配了就默认展开（`windowTouched` 之后听用户的，那是另一回事）。
    assert "if (!row.dataset.windowTouched) windowToggle.checked = scheduled;" in body
    # ③ 折叠态自己就说得清「配了没配」。
    assert "已配定时 · 开启 " in body
    assert "'未设定时'" in body


def test_the_schedule_window_starts_folded(client: TestClient) -> None:
    """默认折起来——这就是省下来的那几行。

    和上一条是一对：那一条守「配了的不许藏」，这一条守「没配的不许占地方」。
    """
    body = _page_body(client.get("/missions").text)

    assert "window_.hidden = true;" in body
    assert "'mission-window-toggle'" in body


def test_folding_the_schedule_window_never_writes_anything_to_the_server(
    client: TestClient,
) -> None:
    """折叠只是这一刻的视图，不是配置。

    ⚠️ 折叠开关和「参与调度」那个复选框都是 `input[type=checkbox]`，走的又是同一个
    `change` 处理器。它这一支要是没抢在前面并且自己 `return`，折一下就会打出一次
    PATCH——而「折一下把任务停了」是这里最糟的一种失败。
    """
    body = _page_body(client.get("/missions").text)

    fold = body.index("if (event.target.matches('.mission-window-toggle')) {")
    enabled = body.index("if (event.target.matches('.mission-enabled')) {")
    assert fold < enabled, "折叠开关那一支排到了「参与调度」后面，会顺带 PATCH 一次"
    # 这一支自己收尾，绝不往下落。
    branch = body[fold:enabled]
    assert "return;" in branch
    assert "patch(" not in branch


def test_the_fold_toggle_still_works_while_the_scheduler_is_running(
    client: TestClient,
) -> None:
    """运行中最想知道的恰恰是「这一轮的定时配的是什么」。

    这一页运行中会把行里所有 `input` 一并置灰（配置已固化，后端也会 409）。
    折叠开关跟着灰掉的话，一个折起来的定时在运行中就再没有办法展开去看——
    而展开只是去看，里面那两个输入框仍然是灰的。
    """
    body = _page_body(client.get("/missions").text)

    lines = [line for line in body.splitlines() if ".mission-window-toggle')" in line]
    assert any("disabled = false" in line for line in lines), "折叠开关跟着锁一起灰掉了"


def test_the_bot_row_carries_a_new_round_button(client: TestClient) -> None:
    """bot 打完一轮就退出调度，**不自动开下一轮**——开新一轮只能是用户按的。

    按钮只长在 bot 那一类行上（海盗与扫描没有「一轮」这个概念），而接口按
    **任务 id** 寻址：同一 kind 可以有多个任务，各开各的轮，写死 `/BOT/` 会把
    两个任务的轮一起推掉。
    """
    body = _page_body(client.get("/missions").text)

    assert "重开一轮" in body
    assert "if (task.kind === 'BOT') {" in body
    assert "`/api/missions/${taskId}/new-round`" in body


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


def test_the_page_waits_for_a_poll_before_scheduling_another(client: TestClient) -> None:
    """慢的调度快照不能被固定间隔的下一次 GET 叠加。"""
    body = _page_body(client.get("/missions").text)

    assert "function scheduleNextPoll()" in body
    assert "poll().finally(() => window.setTimeout(scheduleNextPoll, 2000))" in body
    assert "setInterval(refresh, 2000)" not in body
    assert "setInterval(refreshBackfill, 2000)" not in body


def test_the_scheduler_view_short_cache_coalesces_concurrent_readers(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """页面与悬浮台同时问状态时，只计算一份重快照。"""
    console = client.app.state.mission_console
    scheduler = client.app.state.mission_scheduler
    console._invalidate_scheduler_view()  # noqa: SLF001 - precisely the cache under test
    now = [10.0]
    calls = 0
    original_snapshot = scheduler.snapshot

    def counted_snapshot():  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return original_snapshot()

    monkeypatch.setattr(console, "_monotonic", lambda: now[0])
    monkeypatch.setattr(scheduler, "snapshot", counted_snapshot)

    console.scheduler_view()
    console.scheduler_view()
    assert calls == 1

    now[0] += 1.0
    console.scheduler_view()
    assert calls == 2


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


def test_the_page_disables_every_edit_control_while_running(client: TestClient) -> None:
    """运行中把输入框、复选框、拖拽把手一并置灰。

    后端已经 409 拒了（`tests/integration/api/test_scheduler_api.py`），但只拒不
    置灰的话，用户要改完、按下回车、看见一条红字才知道白改了。页面这一侧是同一
    条规则的**提前显形**，不是第二份判据——灰不灰由接口下发的 `config_locked`
    决定，页面不自己判断调度器在不在跑。
    """
    body = _page_body(client.get("/missions").text)

    assert "config_locked" in body
    for control in (".mission-param", ".mission-enabled"):
        assert control in body, control
    # 拖拽认的是 `draggable` 属性，所以锁上必须真的去改它，不能只把把手画灰。
    assert "setAttribute('draggable'" in body
    assert "disabled = locked" in body


def test_the_page_says_why_the_controls_are_grey(client: TestClient) -> None:
    """只置灰不解释，用户只会得出「这页坏了」。"""
    body = _page_body(client.get("/missions").text)

    assert "运行中" in body
    assert "结束" in body
    # 「恢复」那条口子也得说出口：它是运行中唯一还能按的按钮。
    assert "恢复" in body


def test_the_revive_button_survives_the_lock(client: TestClient) -> None:
    """一条链路可能在调度器跑着的时候被自动停用，那时用户最需要恢复它。

    显隐只看 `status === '已停用'`，**不看锁**——跟着锁一起藏起来的话，运行中
    被自动停用的链路在页面上就再没有恢复的办法。
    """
    body = _page_body(client.get("/missions").text)

    lines = [line for line in body.splitlines() if ".mission-revive" in line]
    assert lines, "页面上没有恢复按钮了"
    assert any("已停用" in line for line in lines), "恢复按钮的显隐不再看「已停用」"
    for line in lines:
        assert "locked" not in line, line
        assert "disabled" not in line, line


def test_the_page_shows_the_frozen_configuration_record(client: TestClient) -> None:
    """「记录任务内容」得有个看得见的入口，否则记了也等于没记。"""
    html = client.get("/missions").text

    assert "配置固化记录" in html
    assert "与上一次相比" in html
    # 记录落在磁盘上的位置写出来：控制台没开也要查得到。这里是夹具注入的那个
    # 临时文件名；生产默认走 `DEFAULT_FREEZE_LOG`，由
    # `test_the_console_writes_its_freezes_under_var` 钉住。
    assert "freezes.jsonl" in html


def test_the_frozen_record_table_lists_only_the_tasks_that_take_part(
    client: TestClient,
) -> None:
    """历史那张表同样只摆参与调度的任务。用户口径 2026-08-17。

    断言钉的是**整张清单与条数**，不是「不含某个名字」：只查名字的话，把过滤写成
    「漏掉某一条」照样绿。
    """
    # 种子：海盗与 bot 不参与，扫描与军力榜参与。把海盗打开、扫描关掉，这一轮
    # 参与的恰好是海盗与军力榜——两个 kind 都不是种子里的默认状态。
    _patch_task(client, "PIRATE", {"enabled": True})
    _patch_task(client, "SCAN", {"enabled": False})
    # `reconcile: false`：默认的启动对账会去真的 Popen 一个补录进程。
    assert client.post("/api/scheduler/start", json={"reconcile": False}).status_code == 200

    cell = _freeze_table_cell(client.get("/missions").text)

    assert cell.count("· 参与 ·") == 2
    assert "未参与" not in cell
    for label in ("侦查+攻击海盗", "扫描军力榜"):
        assert label in cell, label
    for label in ("扫描+攻击 bot", "扫描全星系 bot"):
        assert label not in cell, label


def _patch_task(client: TestClient, kind: str, payload: dict[str, object]) -> None:
    """按 kind 找到那一行再 PATCH。接口按 id 寻址（同一 kind 可以有多行）。"""
    tasks = client.get("/api/scheduler").json()["tasks"]
    task_id = next(task["task_id"] for task in tasks if task["kind"] == kind)
    response = client.patch(f"/api/missions/{task_id}", json=payload)
    assert response.status_code == 200, response.text


def _freeze_table_cell(html: str) -> str:
    """固化记录表里「当时的配置」那一格。

    整页搜不行：页面底部那段脚本自己带着 `未参与` 的字面量（本轮已固化那块
    卡片由它渲染），整页搜会被它满足，断言就永远绿。
    """
    start = html.find('<h2 id="freeze-head">')
    assert start != -1, "页面上没有配置固化记录这一节了"
    end = html.find("</table>", start)
    assert end != -1, "固化记录那张表的结构变了，这个切法得跟着改"
    return html[start:end]


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
