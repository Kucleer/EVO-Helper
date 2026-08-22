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


def test_the_scheduler_and_the_backfill_share_one_compact_top_bar(
    client: TestClient,
) -> None:
    """调度器与战报补录压成顶部一条。

    ⚠️ 旧断言钉的是 `missions-control-grid`（并排两张面板）。2026-08-22 改版把
    它们合成一张 `.missions-topbar`：那两块常驻显示的东西加起来只有一行，却吃掉
    整整两屏顶部，而任务卡才是这一页的主体。旧断言不再成立**不是因为措辞变了**，
    是因为那个两栏网格真的没有了。

    ⚠️ **压扁的只是常驻那一行。** 补录那三块「跑起来才显形」的东西必须仍在同一
    张面板里：补录最坏跑十几分钟，除了它们页面上没有别的进度来源，而
    2026-08-19 那次事故正是「紧凑化顺手删了展示节点」。
    """
    body = _page_body(client.get("/missions").text)

    assert "missions-topbar" in body
    assert "missions-control-grid" not in body
    # 两件事仍然分得开：各自一个 `<h2>`，各自一个 ⓘ。
    assert 'id="sched-head"' in body
    assert 'id="backfill-head"' in body
    assert 'class="tips"' in body
    # 补录跑起来才显形的三块，一块都不许少。
    for node in ('id="backfill-summary"', 'id="backfill-log"', 'id="backfill-log-path-line"'):
        assert node in body, node


def test_the_military_first_switch_moved_into_the_fold_without_being_deleted(
    client: TestClient,
) -> None:
    """「军力优先」从主行挪进「更多」折叠区，**没有被删掉**。

    ⚠️ 旧断言钉的是 `taskCell.append(modeLabel)`（它长在任务名那一格里）。
    用户口径 2026-08-22 只说了「主行上不需要这个按钮」——删掉它等于从界面上
    删掉一种运行模式：关掉之后选靶换成「按坐标顺序、按范围打」那条分支。
    所以这里钉的是「进了折叠区」而不是「不见了」。

    默认开，而且这个默认**落在库里**（新任务的 `params_json` 就带
    `by_military`），不是页面上一个勾好看的复选框——页面显示勾着、库里却是
    另一套，就是一张卡说一套、调度器做另一套。
    """
    body = _page_body(client.get("/missions").text)

    assert "makeInput('military-enabled', { type: 'checkbox'" in body
    # 它现在挂在「更多」那一层里（`modeRow` 被 append 进 `more`），不在主行上。
    assert "modeRow.append(modeLabel" in body
    assert "more.append(modeRow)" in body
    assert "taskCell.append(modeLabel)" not in body
    # 多出发点方案同样搬进折叠区，但仍然是这张卡自己的东西。
    assert "militaryDispatch.className = 'military-dispatch more-line'" in body
    assert "more.append(militaryDispatch)" in body


def test_the_save_button_stays_reachable_when_military_first_is_switched_off(
    client: TestClient,
) -> None:
    """⚠️ 「保存军力方案」不许跟着开关一起藏。

    它存的是整份军力方案，**包括开关本身**（`saveMilitary` 里 `by_military:
    enabled`，关着的时候只送这一个键）。跟着 `.military-dispatch` 一起藏起来的话，
    用户把开关拨到「关」之后就再没有任何按钮能把这个状态存下来——那等于这个开关
    只能开不能关，而「把它留下来」的全部意义就是那条路还走得通。
    """
    body = _page_body(client.get("/missions").text)

    # 按钮长在开关那一行（`modeRow`），不在会被 `setMilitaryVisible` 藏掉的
    # `.military-dispatch` 里。
    assert "modeRow.append(modeLabel, makeTips('军力优先说明', MILITARY_MODE_TIP), save)" in body
    assert "militaryDispatch.append(originHead, origins)" in body
    # 只藏多出发点那一组，按钮那一行不动。
    assert "militaryDispatch.hidden = !enabled" in body
    assert "save.disabled = locked" in body


def test_a_new_task_is_created_military_first(client: TestClient) -> None:
    """默认开这件事由库说了算：新建出来的任务 `params.by_military` 就是 true。

    页面上那个复选框只是回显（`task.params.by_military === true`）。默认写在
    页面上的话，用户看到勾着、调度器却按坐标顺序打——而范围那三个字段已经不在
    页面上渲染了，他连改回去的地方都找不到。
    """
    created = client.post("/api/missions", json={"kind": "BOT", "origin": "5:261:8"})

    assert created.status_code == 201, created.text
    assert created.json()["params"]["by_military"] is True


def test_the_scan_row_is_not_draggable_and_says_why(client: TestClient) -> None:
    """扫描恒在最后一位，页面上就不能给它一个能拖的把手。

    它永远有活干，排在谁前面谁就永远轮不到——拖到海盗之前等于当天 32 次
    配额悄无声息地全流失。后端会拒（带 priority 的 PATCH 返回 400），
    但用户不该拖完了才发现。

    **卡片现在由页面脚本按 `/api/scheduler` 下发的任务列表建**（同一 kind 可以有
    多张，服务端渲染不出固定几张），所以断言落在建卡那一段的判据上：
    `draggable` 取值只由「这条链路填不填空隙」决定，而且拖动那一路还要再挡一次
    「把别人拖到它后面」。

    ⚠️ 判据从「是不是 SCAN」换成了 `FILLS_GAPS`（2026-08-22 改版），钉的行为
    没变、还多守了一条：军力榜（RANKING）同样恒在最后一位，带 priority 的 PATCH
    打到它身上也是 400，而旧写法让它是可拖的——拖一下就吃一个 400。
    """
    body = _page_body(client.get("/missions").text)

    # 与 `domain.scheduler.GAP_FILLERS` 同一批。
    assert "const FILLS_GAPS = ['SCAN', 'RANKING'];" in body
    # 建卡时：填空隙的不可拖，别的都可拖。写成常量 'false' 就等于全都不能拖。
    assert "row.setAttribute('draggable', fillsGaps ? 'false' : 'true')" in body
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


# -- 2026-08-22 改版：任务卡、自动命名、次要链路沉底 ---------------------------


def test_the_bot_card_no_longer_renders_the_three_range_boxes(client: TestClient) -> None:
    """军力攻击卡上不再有「星系 / 系号 / –」那三个范围框。

    依据在 `application/mission_scheduler.py` 的选靶分支：

        if _bot_by_military(params_json):
            return most_valuable_first(...)        ← 军力优先走这条，不看范围
        in_range = bot_targets_in_range(..., **_bot_range(params_json))

    也就是说军力优先模式下那三个字段**一次都没被用到**。一个填了不生效的输入框
    比没有更糟——用户会以为自己把这一轮的范围配好了。
    """
    body = _page_body(client.get("/missions").text)

    # 主行的渲染源是 `PARAM_FIELDS`；BOT 那一档清空，主行就长不出范围框。
    assert "BOT: []," in body

    # ⚠️ **但三个字段本身必须还在页面上**，在「更多」里、只在军力优先关掉时显形。
    # 第一版把它们整个撤掉了，于是留下一个陷阱：开关能关，关掉之后后端因为缺
    # `galaxy` 直接 400，而页面上没有任何地方能填它——那等于把一种运行模式
    # 删掉了，只是删得不明说。见
    # `test_turning_military_priority_off_leaves_somewhere_to_put_the_range`。
    for key in ("'galaxy'", "'first_system'", "'last_system'"):
        assert key in body, f"{key} 从页面上消失了：关掉军力优先之后就没处填了"
    # 判据是「不在主行」，而不是「不在页面上」：范围行必须建在主行之后。
    main_line = body.index("line.append(makeField('出发 '")
    range_row = body.index("rangeRow.className = 'more-line mission-range'")
    assert main_line < range_row, "范围行跑到主行里去了"


def test_the_backend_still_keeps_the_range_fields_the_page_stopped_rendering(
    client: TestClient,
) -> None:
    """**只是不渲染，后端一个字段都没删。**

    非军力优先那条分支还在跑（「军力优先」开关只是挪进了折叠区），存量任务的
    `params_json` 里也还存着这三个值，配置固化记录要认得出它们才念得出「改了
    什么」。这一条就是钉住「页面撤控件」没有顺手变成「后端删字段」——那会让
    一批已经存在的任务在下一次启用时静默改变打法。
    """
    from evo_helper.application.mission_scheduler import _bot_range
    from evo_helper.domain.missions import bot_targets_in_range
    from evo_helper.web.display import PARAM_LABELS

    assert _bot_range('{"galaxy": 5, "first_system": 100, "last_system": 120}') == {
        "galaxy": 5,
        "first_system": 100,
        "last_system": 120,
    }
    assert callable(bot_targets_in_range)
    for key in ("galaxy", "first_system", "last_system"):
        assert key in PARAM_LABELS, key


def test_the_two_chains_that_never_dispatch_have_no_origin_or_fleet_line_control(
    client: TestClient,
) -> None:
    """**不派遣的两条链路**都不给出发点与航线两个控件。

    两个签名都不接它们：

        domain.missions.scan_command()     -> def scan_command() -> list[str]
        domain.missions.ranking_command()  -> 只吃 bot_limit / blind_rows

    填进去的值从来没有到达过 runner。一个改了也不生效的输入框比没有更糟：
    用户会以为自己配好了，然后去等一个永远不会变的行为。

    ⚠️ **判据必须是 `fillsGaps` 而不是 `isScan`**。改版第一版写的是后者，
    于是军力榜那张卡照旧长出了这两个死控件——而它和扫描的共同点恰恰是
    「填空隙、不派遣」。这一条就是为了钉住那次疏漏。

    这一条同时钉「为什么没有」也写在了卡上：只把控件拿掉，用户只会觉得这一行
    缺了东西。
    """
    body = _page_body(client.get("/missions").text)

    assert "if (fillsGaps) {" in body, "判据退回 isScan 的话，军力榜会长出死控件"
    assert "scan_command() 一个参数都不接" in body
    assert "ranking_command() 只吃「扫描数量」和「盲滚行数」" in body
    # 出发点与航线只在 `else` 那一支里建，所以建卡函数里它们和这个判断是互斥的。
    origin = body.index("origin.className = 'mission-origin'")
    branch = body.index("if (fillsGaps) {")
    assert branch < origin, "出发点控件跑到判断前面去了，两条链路会跟着长出来"


def test_the_pirate_and_the_full_scan_sit_in_the_bottom_section(client: TestClient) -> None:
    """海盗与全星系扫描沉到页面底部的「其他链路」。

    ⚠️ **压暗不是停用。** 它们仍然可开关、参数照常改（海盗的「半径」就在卡上）
    ——把还在跑的链路做成看不见，用户就会以为它没在跑，然后去别处找原因。
    """
    body = _page_body(client.get("/missions").text)

    assert "missions-secondary" in body
    assert '<div id="other-cards"></div>' in body
    # 分节表：海盗与扫描落在同一块次要区，bot 与军力榜各有自己的一块。
    assert "PIRATE: document.getElementById('other-cards')" in body
    assert "SCAN: document.getElementById('other-cards')" in body
    assert "BOT: document.getElementById('mission-cards')" in body
    assert "RANKING: document.getElementById('ranking-cards')" in body
    # 海盗的半径没有跟着搬走。
    assert "PIRATE: [{ key: 'radius'" in body


def test_the_ranking_scan_gets_its_own_card_that_says_it_never_dispatches(
    client: TestClient,
) -> None:
    """军力榜扫描单独一张卡，摆在军力攻击下面，并说明它不派舰队。

    它是上面那一节读数的唯一来源（「军力 ÷ 往返小时」里的军力就是它采回来的），
    但 `ranking_command()` 里没有 `--attack`——结构上就没有派舰队的能力。
    调它的参数改的其实是攻击的选靶质量，所以它挨着攻击摆，而不是和海盗、
    全星系扫描混在一起。
    """
    body = _page_body(client.get("/missions").text)

    assert "军力榜扫描" in body
    assert "攻击的读数来源 · 它不派舰队" in body
    # 参数一个没少。
    assert "key: 'bot_limit'" in body
    assert "key: 'scan_cooldown_hours'" in body


def test_the_new_task_row_has_no_name_box_and_never_sends_a_name(
    client: TestClient,
) -> None:
    """任务名不再手输：新建那一行没有名字输入框，POST 也不带 name。

    名字由服务端按出发点的银河系派生（`5:261:8` → `5系攻击`，重名加序号，见
    `web.persistent_service._auto_mission_name`）。页面自己算一份的话，两处规则
    迟早分家——而名字正是日志、运行历史、配置固化记录里认人的那个字段，分家之后
    页面显示的和日志里写的就不是同一个名字了。
    """
    body = _page_body(client.get("/missions").text)

    assert 'id="new-task-name"' not in body
    assert "'new-task-name'" not in body
    # 新建那一路只送 kind / origin / fleet_lines。
    payload = body[body.index("document.getElementById('btn-new-task').onclick") :]
    payload = payload[: payload.index("EVOHelper.request('POST', '/api/missions', payload)")]
    assert "name:" not in payload
    assert "名字自动取" in body


def test_the_page_shows_the_derived_name_and_numbers_the_duplicates(
    client: TestClient,
) -> None:
    """自动命名与重名加序号，走的是真的接口。

    ⚠️ 名字必须**真的写进库**：只在页面上显示的话，页面写着「5系攻击」、日志里
    还是旧名字，两边对不上——而这个字段存在的全部意义就是让两边对得上。
    这里查的是 `/api/scheduler` 下发的 label，也就是任务行里存着的那个名字。
    """
    for origin in ("5:261:8", "5:250:3", "7:228:15"):
        response = client.post("/api/missions", json={"kind": "BOT", "origin": origin})
        assert response.status_code == 201, response.text

    labels = [task["label"] for task in client.get("/api/scheduler").json()["tasks"]]

    assert "5系攻击" in labels
    assert "5系攻击 2" in labels
    assert "7系攻击" in labels


def test_the_run_history_and_the_frozen_record_start_folded(client: TestClient) -> None:
    """运行历史与配置固化记录默认折起来：它们是事后翻的，不是盯着看的。

    ⚠️ 折的是**整节**，而不是节里的某几行：用户要看的时候一次全展开，
    不用一行一行去点。折起来之后节标题仍然在页面上，所以「这里有这么一份记录」
    还是看得见的——那是它们唯一必须常驻的信息。
    """
    body = _page_body(client.get("/missions").text)

    for head in ('<h2 id="runs-head">', '<h2 id="freeze-head">'):
        opening = body.rfind("<details", 0, body.index(head))
        assert opening != -1, head
        # `<details>` 不带 `open`，也就是默认折着。
        assert "open" not in body[opening : body.index(head)], head


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


def test_turning_military_priority_off_leaves_somewhere_to_put_the_range(
    client: TestClient,
) -> None:
    """⚠️ 关掉「军力优先」之后，范围三字段**必须够得着**。

    改版第一版把范围框整个撤掉了，理由是军力优先模式下 `most_valuable_first`
    压根不看它们——那是对的。但那样就留下一个陷阱：

        开关能关 → 关掉走 `nearest_first(bot_targets_in_range(...))`
                 → 后端缺 `galaxy` 直接 400
                 → **而页面上没有任何地方能填它**

    也就是「开关只能开不能关」，等于那条运行模式被删掉了，只是删得不明说。
    留着一个按下去必然报错、又无法补救的开关，比干脆删掉更坏。

    所以三个字段回到「更多」里，跟着开关**反向**显隐。这一条钉的就是这件事。
    """
    body = _page_body(client.get("/missions").text)

    # 范围行建在「更多」里，而且是 BOT 那一档的三个字段。
    assert "mission-range" in body
    assert "range.hidden = enabled" in body, "范围行必须与开关反向显隐"
    assert "BOT_RANGE_FIELDS" in body, "范围字段要有单独的定义表"
    # ⚠️ 键名必须是 `dataset.param`：保存与回显两处认的都是这个属性。写成
    # `dataset.key` 的话，框看着能填、值永远存不进去——第一版就踩了这个。
    assert "input.dataset.param = field.key" in body
    # 主行上不许再出现它们：用户要的是「主行只有出发/航线/三个军力参数」。
    line = body.index("line.append(makeField('出发 '")
    fold = body.index("rangeRow.className = 'more-line mission-range'")
    assert line < fold, "范围行跑到主行前面去了"
