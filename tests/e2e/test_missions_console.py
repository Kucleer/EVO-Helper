"""调度台页面。

断言全部打在 `/missions` 返回的 HTML 上：这一页的行为几乎都在模板与它自带的
那段脚本里，取不到 HTML 就什么都守不住。

**这里不真的 Popen 任何 runner**：`launch` 一律注入假的，后台 tick 推到一小时
一次。真起一个会去点用户的真实鼠标、派真实舰队。
"""

from __future__ import annotations

import re
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


def test_the_military_first_switch_is_gone_from_the_page(client: TestClient) -> None:
    """「军力优先」那个开关**整个从页面上删掉了**。

    ⚠️ **这一条推翻了它自己的两个前身，来回的理由必须留着**，否则下一个人会以为
    是随手改的：
    ① 最早它长在主行任务名那一格里（`taskCell.append(modeLabel)`）；
    ② 2026-08-22 用户说「主行上不需要这个按钮」，于是挪进「更多」——那时**不能**
       删，因为关掉它意味着换一条真的还在跑的选靶分支
       （`nearest_first(bot_targets_in_range(...))`），删了等于从界面上删掉一种
       运行模式；
    ③ 同日用户又说「目前原来的攻击模式已经被废弃了，前端页面不需要兼容」——
       那条分支不再是「另一条走得通的路」，于是开关连同范围三字段一起撤掉。

    ⚠️ **后端一个字段都没删**：`by_military` 仍然在库里、仍然由调度器读，新任务
    落库仍然是 `true`（另有用例钉着）。页面只是不再提供切换入口。

    军力方案（出发点 / 航线）**不再跟任何开关显隐**——它是这条链路唯一的派遣依据，
    藏在一个条件后面只有「还有另一条路」时才说得通。它此刻在主行右侧
    （2026-08-22 第三轮改版，另有用例钉着落点）。
    """
    body = _page_body(client.get("/missions").text)

    # ⚠️ 断言打在**代码构造**上，不是裸字符串 `military-enabled`：那个类名如今只
    # 出现在几句「这里从前有个开关」的注释里，而那几句注释是有意留下的（来回改过
    # 三次的东西，理由不留在原地下一个人就会再改回来）。整页搜会被自己的注释喂饱。
    assert "makeInput('military-enabled'" not in body, "开关又长回来了"
    assert "querySelector('.military-enabled')" not in body, "还有代码在读那个开关"
    assert "matches('.military-enabled')" not in body
    assert "modeRow" not in body
    assert "军力优先说明" not in body
    # 军力方案恒显示（没有 `hidden = true` 那一句了）。
    #
    # ⚠️ 旧断言钉的是 `militaryDispatch.className = 'military-dispatch more-line'`
    # 和 `more.append(militaryDispatch)`——也就是「它在折叠区里」。2026-08-22 第三轮
    # 改版把它提到了主行右侧（用户口径：「下拉框放在右侧，减少一行」），`more-line`
    # 这个类名跟着去掉了（那条布局规则属于折叠区，主行里借不到）。「恒显示」这一条
    # 本身没变，所以只换落点、不换判据；落点由
    # `test_the_military_plan_sits_in_the_main_line_not_behind_a_fold` 单独钉。
    assert "militaryDispatch.className = 'military-dispatch'" in body
    assert "line.append(dispatchSpacer, militaryDispatch)" in body
    assert "militaryDispatch.hidden = true" not in body


def test_the_score_window_boxes_are_gone_from_the_task_row(client: TestClient) -> None:
    """「读数有效期」和「窗口门限」两个框**整个从任务行上撤掉了**（2026-08-23）。

    用户口径（2026-08-23）：「军力攻击的有效期 门限 改为全局设置，不再根据单个星系
    进行调整」。任务页改版之后一个任务对应一个出发点银河系，那两个框留在这一行上
    就是「按星系分别配」的入口。

    ⚠️ **撤掉框，不是把它们置灰、也不是让它们变成只读。** 一个填得进去（或者
    看得见一个数）却不生效的框，是这条链路每一次事故共同的形状——用户改了、
    看着像生效了，而判据读的是另一个数。

    ⚠️ **「军力上限」必须留着**，它仍然是任务级的：上限取决于这个任务用哪个预设
    出击，而预设是按任务配的。删掉它这条用例同样要红，否则「撤掉两个框」很容易顺手
    把三个都撤了。

    ⚠️ 断言打在**代码构造**上，不是裸类名：那两个类名如今只出现在几句「这里从前有
    两个框」的注释里，而那几句注释是有意留下的。整页搜会被自己的注释喂饱。
    """
    body = _page_body(client.get("/missions").text)

    assert "makeInput('military-score-max-age'" not in body, "有效期那个框又长回来了"
    assert "makeInput('military-top-n'" not in body, "窗口门限那个框又长回来了"
    assert "querySelector('.military-score-max-age')" not in body, "还有代码在读那个框"
    assert "querySelector('.military-top-n')" not in body, "还有代码在读那个框"
    # 军力上限**仍然是任务级的**，一个字都没动。
    assert "makeInput('military-max-score'" in body, "军力上限被顺手一起撤了"
    assert "makeField('军力上限 ', maximum)" in body
    # 保存时把那三个已失效的键从 `params_json` 里清掉——否则后端每一轮派遣都要为
    # 它们打一条 WARNING，而那条告警必须有尽头，不然等于没有告警。
    assert "delete payload.top_n;" in body
    assert "delete payload.score_max_age_hours;" in body
    assert "delete payload.rescan_after_hours;" in body
    # 页面要说清那两格搬去哪了：用户是在这一行上找不到它们之后才去找的。
    assert "攻击配置" in body


def test_the_save_button_sits_with_the_rows_it_saves(client: TestClient) -> None:
    """「保存军力方案」摆在它要保存的那几行旁边，并且运行中会被锁上。

    ⚠️ 旧断言钉的是「按钮长在开关那一行、不许跟着 `.military-dispatch` 一起藏」
    ——理由是 `saveMilitary` 当时要能把「军力优先＝关」这个状态存下来，跟着藏起来
    就等于那个开关只能开不能关。开关随「原来的攻击模式被废弃」删了（用户口径
    2026-08-22），那个理由随之消失：军力方案不再藏，按钮也就没有「被藏掉」这回事。

    于是它挪到出发点那一组旁边——「改完这几行、就在这几行旁边按保存」。
    `by_military` 仍然显式送 `true`（存量任务里可能存着 `false`，而页面上再也没有
    别的地方能把它掰回来）。

    ⚠️ 旧断言钉的是 `document.createTextNode(' '), save)`——那时按钮长在一个叫
    `originHead` 的 `<div>` 里，靠裸空白文本节点分隔。2026-08-22 第三轮改版把整块
    提到主行右侧并改成一个 `flex` 容器（标签 / 列表 / 按钮各是一个 flex 项），而
    **纯空白文本节点在 flex 容器里根本不渲染**，间距一直是 `gap` 在给。所以那两个
    「间隔」删了，断言换成钉「保存按钮和这一组出发点是同一次 append 的邻居」。
    """
    body = _page_body(client.get("/missions").text)

    assert "save.className = 'btn primary small military-save'" in body
    assert "makeTips('多出发点说明', MILITARY_ORIGIN_TIP), save);" in body
    assert "militaryDispatch.append(originHead, origins, addOrigin," in body
    assert "by_military: true" in body, "恒送 true：存量任务里那个 false 得有地方被纠正"
    # 运行中配置已固化，按钮不该点下去才发现（后端也会 409）。
    assert "save.disabled = locked" in body


def test_the_military_plan_sits_in_the_main_line_not_behind_a_fold(
    client: TestClient,
) -> None:
    """军力方案在**主行右侧**，不在折叠里（用户口径 2026-08-22）。

    ⚠️ **这一条推翻了它的前身，来回的理由必须留着。** 上一版把它放进「更多」，
    理由写的是「配一次就不再动」。那句话对定时开关成立，对它不成立：它是这条链路
    唯一的派遣依据（`_military_assignments` → `_military_origins` →
    `_configured_origins`），加一颗星球、改一个航线数都得先点开一层折叠才够得着，
    而主行那段只读文字只念结果、改不了。

    落点是主行最右侧：摘要钉了 640px 的上界，它右边本来就是一整片留白。用户口径
    「下拉框放在右侧，减少一行」减掉的那一行，正是从前常驻的「更多」折叠条。

    ⚠️ **`flex-wrap`，不是绝对定位。** 多出发点时这一块是一份多行列表、高度不定；
    定位硬塞的话行数一多就会盖住底下的东西，宽度算错还会撑出横向滚动条。
    """
    body = _page_body(client.get("/missions").text)
    css = _console_css()

    # 落在主行上，前面带一个 `.spacer` 把它推到右边。
    assert "dispatchSpacer.className = 'spacer'" in body
    assert "line.append(dispatchSpacer, militaryDispatch)" in body
    assert "more.append(militaryDispatch)" not in body, "又被塞回折叠区了"
    # ⚠️ 排在「更多」**之前**：`.mission-more[open]` 会撑成整行，排在它后面的话
    # 一展开就把军力方案挤到更下面一行去。
    assert body.index("line.append(dispatchSpacer, militaryDispatch)") < body.index(
        "line.append(more);"
    ), "军力方案排到「更多」后面去了，展开一次就被挤下去"

    dispatch = _rule_block(css, ".military-dispatch {")
    assert "display: flex" in dispatch, "改回 display: block 就又是两行"
    assert "flex-wrap: wrap" in dispatch, "多出发点时会撑出横向滚动条"
    # 绝对定位是明确否掉的方案。
    assert "position: absolute" not in dispatch
    # 每颗出发点一行、纵向排：两颗并排就已经比主行右侧那块空白宽。
    assert "flex-direction: column" in _rule_block(css, ".military-origin-list {")


def test_the_fleet_lines_box_is_not_grouped_with_the_origin_dropdown(
    client: TestClient,
) -> None:
    """航线数是**独立控件**（用户口径 2026-08-22：「航线设置数量不要在下拉内」）。

    `.fld` 只圈住「数字框 + 条」这一格——「条」是航线数自己的单位，不是下拉框的
    一部分。下拉框裸着排在前面，两者之间的距离由 `.military-origin-line` 的 `gap`
    给。把两个控件包进同一个 `.fld`（`white-space: nowrap` 的一小格）就等于在页面
    上宣称它们是一件事，而它们是两件：一个是「从哪儿起飞」，一个是「派几条」。
    """
    body = _page_body(client.get("/missions").text)

    assert "line.append(planet, makeField(fleetLines, makeUnit('条')), enable" in body
    assert "makeField(planet" not in body, "下拉框被包进了同一个视觉分组"
    # 控件本身还是那两个，类名不许改（`saveMilitary` 按类名收集这一组）。
    assert "planet.className = 'military-origin-planet'" in body
    assert "makeInput('military-origin-lines'" in body


def test_the_more_fold_no_longer_owns_a_whole_row(client: TestClient) -> None:
    """「更多」的 `<summary>` 是主行里的一小格，不再是常驻的第三行。

    ⚠️ **这一条推翻了它的前身。** 上一版 `<details>` 是 `.mission-card` 的直接
    子节点，带一条横贯整卡的虚线：于是单出发点、没配定时的卡恒定三行高，而那第三
    行里一个值都没有——纯粹是一条写着「更多：……」的横幅。用户口径（2026-08-22）：
    「减少一行。」

    现在它是 `.mission-line` 的 flex 项：折着时宽度由 `<summary>` 的内容定，
    `[open]` 时 `flex: 1 1 100%` 撑成整行、内容往下长。那条虚线也只在展开时才画
    ——折着的时候它横穿主行，会被读成「卡到这里就结束了」。
    """
    body = _page_body(client.get("/missions").text)
    css = _console_css()

    assert "line.append(more);" in body
    assert "row.append(more);" not in body, "又挂回卡片上了，那就是独占一行"
    # 折叠标题不许再列一样已经不在里面的东西。断言打在**完整的那句表达式**上，
    # 不是裸的「军力方案（出发点 / 航线）」——那几个字如今还在几句说明「它为什么
    # 搬走了」的注释里，整页搜会被自己的注释喂饱。
    assert "(task.kind === 'BOT' ? ' · 军力方案（出发点 / 航线）' : '')" not in body
    assert "'更多：定时开关（UTC+8）'" in body

    # 折着的时候不画分隔线（它会横穿主行）。
    assert "border-top" not in _rule_block(css, ".mission-more {")
    # 展开时才独占一行。
    opened = _rule_block(css, ".mission-more[open] {")
    assert "flex: 1 1 100%" in opened
    assert "border-top: 1px dashed var(--border)" in opened


def test_the_expanded_fold_keeps_every_control_it_used_to_hold(
    client: TestClient,
) -> None:
    """展开区里的东西**一个都没丢**。

    这次改版只搬走了军力方案那一块。剩下四个控件（定时的两端、任务级出发点、
    任务级航线数）连同它们的说明文字全都还在——「压掉一行」如果是靠悄悄删控件
    换来的，那就是把用户配好的东西弄没了，而这类故障事后最难看出来。
    """
    body = _page_body(client.get("/missions").text)

    # ① 定时那两端，连同「留空=不限」那句话，都在同一条 `.more-line` 上进折叠区。
    assert "['mission-enabled-from', '开启'], ['mission-enabled-until', '关闭']" in body
    assert "windowNote.textContent = '留空=不限；到点只挡新的一轮，正在跑的不打断'" in body
    assert "more.append(windowRow)" in body
    # ② 任务级出发点与航线数，连同「什么时候才作数」那句话。
    assert "origin.className = 'mission-origin'" in body
    assert "makeInput('mission-lines'" in body
    assert "任务级出发点（只在军力方案一行都没配时才生效）" in body
    assert "more.append(legacy)" in body


def test_a_new_task_is_created_military_first(client: TestClient) -> None:
    """默认开这件事由库说了算：新建出来的任务 `params.by_military` 就是 true。

    ⚠️ 页面上已经**没有**任何地方能改它了（那个复选框随「原来的攻击模式被废弃」
    删掉了，用户口径 2026-08-22）。正因为如此，这个默认更加**必须真的落库**：
    页面显示成军力优先、库里却是「按坐标顺序、按范围打」的话，用户连改回去的
    地方都找不到——而范围那三个字段也不在页面上了。
    """
    created = client.post("/api/missions", json={"kind": "BOT", "origin": "5:261:8"})

    assert created.status_code == 201, created.text
    assert created.json()["params"]["by_military"] is True


def test_the_scan_row_cannot_be_reordered_and_says_why(client: TestClient) -> None:
    """扫描恒在最后一位，页面上就不能给它一对能点的箭头。

    它永远有活干，排在谁前面谁就永远轮不到——拖到海盗之前等于当天 32 次
    配额悄无声息地全流失。后端会拒（带 priority 的 PATCH 返回 400），
    但用户不该点完了才发现。

    **卡片现在由页面脚本按 `/api/scheduler` 下发的任务列表建**（同一 kind 可以有
    多张，服务端渲染不出固定几张），所以断言落在建卡那一段的判据上：
    `data-sortable` 取值只由「这条链路填不填空隙」决定，而且换位那一路
    （`moveCard`）还要再挡一次「把别人换到它后面」。

    ⚠️ 判据原先挂在 `draggable` 上，2026-08-23 拖拽整套换成上下箭头之后改挂
    `data-sortable`（用户口径：「你还是用上下箭头来调整排序把，不要拖拽方案了」）。
    钉的行为一个字没变。

    ⚠️ 判据从「是不是 SCAN」换成了 `FILLS_GAPS`（2026-08-22 改版），钉的行为
    没变、还多守了一条：军力榜（RANKING）同样恒在最后一位，带 priority 的 PATCH
    打到它身上也是 400，而旧写法让它是可排序的——动一下就吃一个 400。
    """
    body = _page_body(client.get("/missions").text)

    # 与 `domain.scheduler.GAP_FILLERS` 同一批。
    assert "const FILLS_GAPS = ['SCAN', 'RANKING'];" in body
    # 建卡时：填空隙的不可排序，别的都可以。写成常量 'false' 就等于全都不能排。
    assert "row.dataset.sortable = fillsGaps ? 'false' : 'true';" in body
    # 换位时只在「可排序」那些卡里找邻居，填空隙的既不参与也不会被换到它后面。
    assert "box.querySelectorAll('.mission-row[data-sortable=\"true\"]')" in body
    # 填空隙的那几张连箭头都不画，只给一个「·」。
    assert "order.title = '填空隙的链路恒在最后一位，不参与排序';" in body
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
    """军力攻击卡上**整页都没有**「星系 / 起始系号 / 结束系号」那三个范围框。

    它们只服务于 `application/mission_scheduler.py` 里那条选靶分支：

        if _bot_by_military(params_json):
            return most_valuable_first(...)        ← 现在只走这条
        in_range = bot_targets_in_range(..., **_bot_range(params_json))
        return nearest_first(in_range, origin)     ← 按范围筛的那条，已废弃

    ⚠️ **这三个框在页面上来回过三次，理由必须留在这里**，否则下一个人会以为是
    随手改的：
    ① 2026-08-22 第一版整个撤掉（军力优先模式下确实一次都用不到）；
    ② 随后发现陷阱——「军力优先」开关能关，关掉走上面第二条分支，后端缺 `galaxy`
       直接 400，而页面上没有任何地方能填它，等于把一种运行模式删掉了、只是删得
       不明说。于是它们回到「更多」里，跟着开关反向显隐；
    ③ 同日用户口径：「目前原来的攻击模式已经被废弃了，前端页面不需要兼容。」
       那条分支不再是「另一条走得通的路」，②那个陷阱随开关一起消失——没有开关，
       就没有「关掉之后没处填」。于是三个框再次撤掉，这一次连「更多」里也不留。

    ⚠️ **后端一个字段都没删**，另有用例（`test_the_backend_still_keeps_...`）钉着。
    """
    body = _page_body(client.get("/missions").text)

    # 主行的渲染源是 `PARAM_FIELDS`；BOT 那一档清空，主行就长不出范围框。
    assert "BOT: []," in body
    # 整页都不该再有这三个键，也不该再有那张单独的字段表和那一行容器。
    for key in ("'galaxy'", "'first_system'", "'last_system'"):
        assert key not in body, f"{key} 又回到页面上了：那条选靶分支已经废弃"
    assert "BOT_RANGE_FIELDS" not in body
    assert "mission-range" not in body


def test_the_backend_still_keeps_the_range_fields_the_page_stopped_rendering(
    client: TestClient,
) -> None:
    """**只是不渲染，后端一个字段都没删。**

    ⚠️ 用户口径（2026-08-22）：「目前原来的攻击模式已经被废弃了，前端页面不需要
    兼容。此部分代码也记录待办，需要清理。」——**前端不兼容，不等于后端可以删。**
    存量任务的 `params_json` 里还存着这三个值，配置固化记录要认得出它们才念得出
    「改了什么」；后端那一轮清理是单独登记的待办，不是这次改页面的副产品。

    这一条就是钉住「页面撤控件」没有顺手变成「后端删字段」——那会让一批已经存在
    的任务在下一次启用时静默改变打法，而「静默改变打法」是这条链路最贵的故障。
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
    # 换位认的是 `data-sortable`，所以锁上必须真的去改它，不能只把箭头画灰。
    assert "row.dataset.sortable = locked || FILLS_GAPS.includes(task.kind)" in body
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


def _console_css() -> str:
    """样式表，**注释先剥掉**。

    ⚠️ 这个仓库里注释比代码长，而注释里成段引用着规则本身。不剥的话，「这条规则
    还在不在」的断言会被一句谈论它的注释喂饱——真把规则删了也照样绿。
    （同 `test_attack_log_military_estimated.py` 里那个同名助手。）
    """
    css = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "evo_helper"
        / "web"
        / "static"
        / "console.css"
    ).read_text(encoding="utf-8")
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def test_the_card_has_exactly_one_place_that_says_where_the_fleet_departs(
    client: TestClient,
) -> None:
    """⚠️ **一张卡上不许有两个「出发点」互相打脸。**

    从前主行摆的是绑在 `mission_tasks.origin_*` 上的下拉框，而军力攻击真正据以
    派遣的是军力方案（`mission_task_origins`）。生产实测（2026-08-22）：#2 号任务
    下拉框显示 `4:277:15`、方案里配的是 `5:261:8`，两个数都在卡上，作数的却是被
    折起来的那一个。

    改法：主行只留一段**只读文字**，念服务端算好的「实际会从哪儿派」；那个下拉框
    连同任务级航线数一起进「更多」，并在那里写明它什么时候才作数。

    ⚠️ **后端字段一个都没删**，控件也没删——海盗从那一颗出发，军力方案一行都没配
    时的 bot 也从那一颗出发（`_configured_origins` 那一档回落），「按星球效率」
    那一页还按它统计。
    """
    body = _page_body(client.get("/missions").text)

    # 主行：只读文字，判据来自服务端下发的 origins。
    assert "dispatch.className = 'mission-dispatch coord'" in body
    assert "function dispatchFacts(task)" in body
    assert "个出发点" in body, "多于一个时主行要报个数"
    # 那个遗留下拉框搬进了「更多」，而且带着「什么时候才作数」这句话。
    legacy = body.index("legacy.className = 'more-line mission-legacy-origin'")
    dispatch = body.index("dispatch.className = 'mission-dispatch coord'")
    assert dispatch < legacy, "遗留下拉框跑回主行前面去了"
    assert "任务级出发点（只在军力方案一行都没配时才生效）" in body
    assert "LEGACY_ORIGIN_TIP" in body
    # 控件本身一个都没少。
    assert "origin.className = 'mission-origin'" in body
    assert "makeInput('mission-lines'" in body


def test_each_galaxy_gets_its_own_card_colour_and_the_number_is_still_written_on_it(
    client: TestClient,
) -> None:
    """每个银河系一套框体配色（用户口径 2026-08-22），**但颜色只做分组**。

    这一页的既有规矩是「状态绝不只用颜色承载」。同一条规矩在这里的落点：
    ① 银河系号本身写在卡上（`.galaxy-tag`）——色相每 9 个银河系循环一次，
       光看颜色分不出 1 系和 10 系；
    ② 启用/停用仍由状态 chip 里那几个字说，`.off` 那一套只是加速。

    色相由页面按号码算进 `--galaxy-hue`，CSS 只认 `[data-galaxy]` 这个挂钩：
    枚举九个类名的话，银河系号没有上界（`Coordinate` 只要求 ≥ 1），漏掉的那些会
    静默落回默认色。
    """
    body = _page_body(client.get("/missions").text)
    css = _console_css()

    assert "galaxy.className = 'galaxy-tag'" in body
    assert "galaxyTag.textContent = galaxies.length ? `${galaxies.join('/')}系`" in body
    assert "row.style.setProperty('--galaxy-hue'" in body
    assert ".mission-card[data-galaxy] {" in css
    assert "border-left-color: hsl(var(--galaxy-hue)" in css
    # 停用的卡：压暗 + 左边框变虚线 + 撤掉那层浅底色，三样一起，
    # chip 那几个字一个没动。
    #
    # ⚠️ **边框转灰那一条已被推翻**（2026-08-22）：第一版写的是
    # `border-left-color: var(--text-faint)`，而实测常态是五张 bot 卡全都
    # 「未启用」——边框一律灰掉之后一屏里银河系分组一点颜色都不剩，
    # 而分组配色正是同一次改版要做的另一半。现在色相保留、只压暗一档，
    # 判据搬到 `test_a_disabled_card_keeps_its_galaxy_hue`。
    off = _rule_block(css, ".mission-card.off {")
    # ⚠️ 压暗**不在**这个块里，它打在兄弟节点上——见下面那条断言。
    # 打在整张卡上的话，那个「参与调度」的勾选框会跟着淡到 60%，
    # 而它是唯一能把一张停用的卡救回来的控件（子元素 opacity 与父级相乘，捞不回来）。
    assert "opacity" not in off, "压暗又打回整张卡了，勾选框会跟着淡掉"
    assert ".mission-card.off > *:not(.mission-enabled) { opacity: 0.6; }" in css
    # ⚠️ 虚线挪到**外框**上，那条银河系色带保持实线（用户口径 2026-08-22
    # 「可以更更明显点」）。虚线打在色带上会把它切成一串短点，而它承载的正是
    # 「这张卡属于哪个银河系」这个一眼就该看见的信息。
    assert "border-style: dashed" in off
    assert "border-left-style: solid" in off
    assert "var(--text-faint)" not in off, "未启用的边框又被灰掉了，银河系分组会整屏消失"
    # ⚠️ **不许洗白银河系标记。** 实测常态是一屏卡全都「未启用」，把标记也去饱和
    # 之后那一屏里银河系分组一点颜色都不剩——而分组配色正是这次要做的另一半。
    assert "grayscale" not in off
    assert "mission-status-word" in body, "状态那个词是启用/停用的主信号，不许被颜色顶掉"


def test_the_units_do_not_stick_to_the_next_label(client: TestClient) -> None:
    """「读数有效期 [6] 小时窗口门限 [200] 个军力上限」——单位和下一个标签黏成了
    一个词（用户口径 2026-08-22：「框体与文字之间增加左右间隙」）。

    ⚠️ **单位必须是元素，不能是裸文本节点。** 裸文本在 flex 容器里只成为一个匿名
    项，前后那点间距全靠容器的 `gap`，没有任何地方能单独给它留白。
    """
    body = _page_body(client.get("/missions").text)
    css = _console_css()

    assert "function makeUnit(text)" in body
    # ⚠️ **原先钉的是「读数有效期 [6] 小时」和「窗口门限 [200] 个」那两格，
    # 它们 2026-08-23 撤掉了**（有效期与窗口门限改成全局设置，搬去了攻击配置页）。
    # 这条用例守的**不是那两个字段**，是「单位必须是元素」这条判据——所以改成钉
    # 现存的那两处单位，而不是跟着删掉整条用例：判据还活着，钉子就得还在。
    assert "makeField('航线 ', lines, makeUnit('条'))" in body
    assert "makeField(fleetLines, makeUnit('条'))" in body
    # ⚠️ 反面也要钉，但只在**那一段**上找：`makeUnit` 上方那段注释里逐字引用着
    # 从前那句裸文本节点，整页搜会被自己的注释喂饱。
    appended = body[body.index("settings.append(") :]
    appended = appended[: appended.index("line.append(settings)")]
    assert "createTextNode" not in appended, "退回裸文本节点就又黏上了"
    # 字段与字段之间的间隙必须明显大于标签与它自己的框之间的距离。
    fields = _rule_block(css, ".regional-params, .military-settings {")
    assert "gap: 6px 18px" in fields
    assert "gap: 6px" in _rule_block(css, ".mission-line .fld {")
    assert "gap: 4px 12px" in _rule_block(css, ".mission-line {")


def test_the_dropdowns_are_big_enough_to_click(client: TestClient) -> None:
    """下拉框加宽加高（用户口径 2026-08-22：「下拉框放大以便点击」）。

    ⚠️ **字号不许跟着缩。** 出发点那个下拉框里装的是坐标，看错一位就是舰队去错
    地方；「放大点击区」不能以「看错一位」为代价换。
    """
    css = _console_css()

    select = _rule_block(css, ".mission-card select {")
    assert "min-height: 30px" in select
    assert "font-size: 13px" in select
    assert "cursor: pointer" in select
    assert "min-width: 210px" in _rule_block(css, ".mission-origin {")
    assert "min-width: 240px" in _rule_block(css, ".military-origin-planet {")


def _rule_block(css: str, selector: str) -> str:
    start = css.index(selector)
    end = css.index("}", start)
    return css[start:end]


def test_a_disabled_card_keeps_its_galaxy_hue() -> None:
    """⚠️ 未启用的卡**仍旧保留银河系色相**，只压暗一档。

    判据是**两个变量占两个通道**：色相回答「哪个银河系」，虚实 + 透明度 + 底色
    回答「参不参与调度」。挤在色相这一个通道上，两件事必然互相抹掉一件。

    这条是踩出来的（2026-08-22）：第一版把未启用的边框换成中性灰
    （`var(--text-faint)`），而实测常态就是**五张 bot 卡全都未启用**——一屏里
    银河系分组于是一点颜色都不剩，而「每个银河系一套配色」正是同一次改版要做的
    另一半。同一条 CSS 规则的注释里当时已经论证过这个后果（论的是
    `filter: grayscale()`），却在下一行把边框自己灰掉了。

    启用/停用**不只靠这些视觉信号**：状态 chip 的文字始终写着（本仓规矩是
    「状态绝不只用颜色承载」）。
    """
    css = _console_css()

    assert ".mission-card.off[data-galaxy]" in css, "未启用态没有单独保留色相的规则"
    # 有银河系的那一支必须留色相；没有银河系的（扫描 / 军力榜）才用中性灰。
    hued = css.index(".mission-card.off[data-galaxy]")
    assert "hsl(var(--galaxy-hue)" in css[hued : hued + 160], "未启用的边框把银河系色相灰掉了"
    plain = css.index(".mission-card.off:not([data-galaxy])")
    assert "var(--text-faint)" in css[plain : plain + 120]
    # 「参不参与调度」得由别的通道承载，否则和色相抢同一个：
    # 外框虚线 + 透明度 + 状态 chip 的文字，而色带本身保持实线、保持色相。
    assert ".mission-card.off > *:not(.mission-enabled) { opacity: 0.6; }" in css, (
        "压暗必须排除勾选框：它是唯一能把停用的卡救回来的控件"
    )
    off = css.index(".mission-card.off {")
    block = css[off : off + 260]
    assert "border-style: dashed" in block, "外框没有虚线，停用就只剩透明度了"
    assert "border-left-style: solid" in block, "虚线打在色带上会把银河系那条线切碎"


def test_disabling_a_card_never_touches_the_galaxy_channel() -> None:
    """⚠️ **停用只许动透明度 / 外框虚实 / 底色浓淡，色相一律不碰。**

    这一条是同一个错误在同一天里犯到第三次之后立的规矩：

    ① `filter: grayscale()` —— 论证过后自己否掉了；
    ② 未启用的左色带换成 `var(--text-faint)` —— 灰掉了；
    ③ 未启用的银河系徽章 `background: none` —— 刚做成实心又洗回透明。

    三次都是同一个形状：**拿「停用」去改「银河系」那个通道**。而实测常态是一屏
    卡全都未启用（生产 2026-08-22：五张 bot 卡无一启用），于是每一次都恰好在
    最需要看清分组的那一屏里，把分组整个抹掉。

    判据不是「别写 grayscale」这种个例，而是**两个变量各占一个通道**：
    色相答「哪个银河系」，其余通道答「参不参与调度」。
    """
    css = _console_css()

    off = _rule_block(css, ".mission-card.off {")
    # 停用能用的三个通道。
    # ⚠️ 压暗不在这个块里：它打在兄弟节点上，好把「参与调度」那个勾选框排除掉。
    assert "opacity" not in off, "压暗打回整张卡了，勾选框会跟着淡到 60%"
    assert ".mission-card.off > *:not(.mission-enabled) { opacity: 0.6; }" in css
    assert "border-style: dashed" in off
    # 色带与徽章都必须另有一条「保留色相」的规则，且不许出现中性灰。
    for selector in (
        ".mission-card.off[data-galaxy]",
        ".mission-card.off[data-galaxy] .galaxy-tag",
    ):
        assert selector in css, selector
        block = _rule_block(css, selector + " {")
        assert "hsl(var(--galaxy-hue)" in block, f"{selector} 把银河系色相丢了"
        assert "--text-faint" not in block, f"{selector} 又灰掉了"
        assert "grayscale" not in block
    # 没有银河系的那两张（扫描 / 军力榜）本来就没有色相可留，才用中性灰。
    plain = _rule_block(css, ".mission-card.off:not([data-galaxy]) .galaxy-tag {")
    assert "background: none" in plain


def test_the_enable_checkbox_is_big_and_sits_in_the_left_gutter(client: TestClient) -> None:
    """⚠️ 「参与调度」那个勾选框放大、并挪到卡片左侧那条留白里、垂直居中。

    用户口径（2026-08-22）：「选中框放大，并放在红框这个位置」。
    原先它挤在第一行 `⠿` 旁边、只有默认的 13px —— 而一张卡三行高，
    **决定「这条链路到底跑不跑」的那个开关是全卡最小最难点的控件**。

    绝对定位而不是改 DOM：它长在 `.mission-line` 里，而那一行是 `flex-wrap` 的，
    留在流里会跟着换行跑到别处去。垂直居中对的是**整张卡**，所以它落在中间那一行
    的高度上、与整卡对齐。

    ⚠️ 停用的卡整体压暗，但这个开关**不跟着淡** —— 它是唯一能把那张卡救回来的
    控件，跟着一起淡掉就等于最该点的东西最看不见。
    """
    css = _console_css()

    rule = _rule_block(css, ".mission-card .mission-enabled {")
    assert "position: absolute" in rule
    assert "top: 50%" in rule
    assert "translateY(-50%)" in rule
    assert "width: 22px" in rule and "height: 22px" in rule
    # 卡片得腾出位置、并且是定位上下文，否则它会飞到 body 左上角。
    card = _rule_block(css, ".mission-card {")
    assert "position: relative" in card
    assert "padding-left" in card
    # 停用时不跟着压暗。
    # ⚠️ 旧断言钉的是 `.mission-card.off .mission-enabled { opacity: 1.6 }`——
    # 那一句既超出 opacity 的取值范围（钳成 1），也不可能反抗父级的 0.6
    # （子元素的 opacity 与父级相乘）。真正的解法是压暗兄弟节点，
    # 而那要求勾选框是卡的**直接子元素**，所以这里连它的 DOM 位置一起钉。
    assert ".mission-card.off > *:not(.mission-enabled)" in css
    body = _page_body(client.get("/missions").text)
    assert "row.prepend(checkbox);" in body, "勾选框不是卡的直接子元素，压暗排除不掉它"


def test_ten_galaxies_never_share_a_hue(client: TestClient) -> None:
    """⚠️ **十个银河系必须十种颜色，而且要留冗余。**

    用户口径（2026-08-22）：「注意最大是 10 个星球，你需要有冗余」。

    第一版写的是 `((galaxy - 1) % 9) * 40` —— 一圈 360° 按 40° 一档，只有 **9**
    种色相。第 10 个银河系恰好绕回第 1 个，而且撞在最坏的位置上：不是「颜色接近」，
    是**一模一样**。差一个，且刚好差在用户说的那个上界上。

    改成黄金角步进 `(galaxy * 137) % 360`：137 与 360 互质，要到第 360 个银河系
    才重复。附带的好处是相邻号码隔 137°，比均分方案（相邻只差一档）更好分。

    ⚠️ **别为了「整齐」改回均分。** 均分的档数 = `360 / gcd(step, 360)`，
    任何一个能整除 360 的步长都会在个位数或十几个之后开始撞车。
    """
    body = _page_body(client.get("/missions").text)

    # ⚠️ 断言打在**那条语句**上，不是裸子串。注释里引用了旧公式作对照，
    # 而注释也是页面正文——整页搜 `% 9) * 40` 会被自己的说明喂饱。
    # （子代理在 `.military-enabled` 上踩过同一个坑，见那条用例。）
    assert "setProperty('--galaxy-hue', `${(galaxies[0] * 137) % 360}deg`)" in body, (
        "色相步进被改回去了，第 10 个银河系会撞色"
    )

    # 把公式在这里算一遍：前 12 个银河系两两不同，最接近的也要拉开肉眼可分的距离。
    hues = [(galaxy * 137) % 360 for galaxy in range(1, 13)]
    assert len(set(hues)) == 12, "12 个银河系里出现了重复色相"
    closest = min(abs(a - b) for index, a in enumerate(hues) for b in hues[index + 1 :])
    assert closest >= 15, f"最接近的两个色相只差 {closest}°，分不开"


def test_reordering_swaps_two_priorities_not_the_whole_block(client: TestClient) -> None:
    """⚠️ **上下箭头一次只换两张卡，发两个 PATCH。**

    用户口径（2026-08-23）：「你还是用上下箭头来调整排序把，不要拖拽方案了」。

    拖拽那一版是「把这一块里原有的那些 priority 值升序发给新的排列」，于是一次
    操作要给**每张卡各发一个** PATCH（生产那一屏 7 张 = 7 个串行请求）。两两交换
    只动两张，也同样不会碰到别的容器里那些任务的相对位置（海盗的 priority 本来
    可能夹在两个 bot 之间）。

    ⚠️ **两个 PATCH 必须串行。** 并发发的话后端按到达顺序写，而这两个值互为对方的
    目标——交叉之后两张卡会一起变成同一个数，次序彻底丢掉。
    """
    body = _page_body(client.get("/missions").text)

    assert "function moveCard(row, delta)" in body
    # 交换的是两张卡原有的那两个值，不是重新发 0..n-1。
    assert "const mine = Number(row.dataset.priority);" in body
    assert "const theirs = Number(other.dataset.priority);" in body
    # 串行：第二个在第一个的 then 里。
    assert "patch(row.dataset.taskId, { priority: theirs })" in body
    assert ".then(() => patch(other.dataset.taskId, { priority: mine }))" in body
    # 只在同一块容器里找邻居。
    assert "const box = row.parentNode;" in body


def test_the_two_arrows_are_real_buttons_wired_to_the_click_delegate(client: TestClient) -> None:
    """箭头是**真按钮**，而且点击走已有的那个委托。

    真按钮换来三样东西，装饰性的 `<span>` 一样都没有：键盘能 Tab 到、
    `:disabled` 有原生语义（禁用时点击根本不派事件）、读屏念得出。

    ⚠️ 守的是拖拽那三轮踩过的坑：靶子必须是个能点的东西。上一版把手只有
    11×23px 的一个字形，收窄起拖点之后实际结果是拖不动。
    """
    body = _page_body(client.get("/missions").text)

    assert "button.type = 'button';" in body
    assert "['mission-up', '▲', '上移']" in body
    assert "['mission-down', '▼', '下移']" in body
    # 读屏念得出：带任务名的 aria-label，不是 aria-hidden 的装饰字符。
    assert "button.setAttribute('aria-label', `${word}「${task.label}」`);" in body
    # 点击走 onCardClick 那个委托。
    assert "if (event.target.closest('.mission-up')) {" in body
    assert "if (event.target.closest('.mission-down')) {" in body


def test_the_arrows_at_both_ends_are_disabled(client: TestClient) -> None:
    """⚠️ **到顶的 ▲ 和到底的 ▼ 要真的禁用。**

    点一个无处可去的箭头会发出「把 priority 设成它自己」的 PATCH：不报错、
    什么都不改，但用户看到的是「点了没反应」——和「坏了」分不开。这一页刚因为
    「点了没反应」被误判成坏了三轮，不该再留一个同形状的坑。

    首尾要**每一块各自算**：海盗摆在另一块里，把两块拼起来算的话，bot 段最后一张
    的 ▼ 会看着可点，而点下去要么越块、要么打到填空隙那几张身上（一律 400）。
    """
    body = _page_body(client.get("/missions").text)

    assert "if (up) up.disabled = index === 0;" in body
    assert "if (down) down.disabled = index === sortable.length - 1;" in body
    # 每一块各自算首尾。
    assert "for (const box of BOXES) {" in body

    css = _console_css()
    # 光淡掉不够，但淡掉也要有——禁用的箭头得看得出来是禁用的。
    assert ".mission-order button:disabled { opacity: 0.25; cursor: default; }" in css


def test_the_arrow_buttons_are_a_target_you_can_actually_hit() -> None:
    """⚠️ **箭头靶子要够点。**

    上一轮的教训：拖拽把手只有 **11×23px** 的一个字形（在真浏览器里量的），
    起拖点从「整张卡」收窄到那个字符之后，实际结果是**拖不动**。

    两个箭头竖排，每个 `min-width: 20px` × `line-height: 13px`，合起来约 20×27
    —— 比那个字形宽，而且是真按钮。
    """
    css = _console_css()

    import re

    block = re.search(r"^\.mission-order button\s*\{[^}]*\}", css, re.MULTILINE)
    assert block is not None, "`.mission-order button` 的规则不见了"
    assert "min-width: 20px" in block.group(0), "箭头没有最小宽度，靶子只有字形那么宽"
    assert "cursor: pointer" in block.group(0)

    column = re.search(r"^\.mission-order\s*\{[^}]*\}", css, re.MULTILINE)
    assert column is not None
    assert "flex-direction: column" in column.group(0), "两个箭头不是竖排的"


def test_the_drag_and_drop_is_gone_for_good(client: TestClient) -> None:
    """⚠️ **整套 HTML5 拖拽必须删干净，别照着旧版本加回来。**

    这一页的拖拽试了三轮都没成：整张卡可拖会把「想选输入框里的文字」变成一次重排；
    收窄到只有把手能起拖之后，那个字形只有 11×23px，实际结果是拖不动；撑大靶子
    并给 CSS 加了缓存指纹之后，用户报的仍是拖不动，而我在真浏览器里复现不出来。

    用户口径（2026-08-23）：「你还是用上下箭头来调整排序把，不要拖拽方案了」。

    留一条用例盯着，是因为「加个把手让它能拖」在这一页上看起来永远像个好主意。
    """
    body = _page_body(client.get("/missions").text)

    # ⚠️ **盯代码，不盯字面提及。** 上面那几段注释里就写着 `dragstart` / `drag-handle`
    # ——那是有意留的「别加回来」的说明，不是残留。所以判据挑的都是只可能出现在
    # 真代码里的形状：函数定义、事件注册、类名赋值、属性写入。
    for gone in (
        "function onCardDragStart",
        "function onCardDragOver",
        "function onCardDragEnd",
        "function onCardPointerDown",
        "addEventListener('dragstart'",
        "addEventListener('dragover'",
        "addEventListener('dragend'",
        "addEventListener('drop'",
        "= 'drag-handle'",
        "setAttribute('draggable'",
    ):
        assert gone not in body, f"拖拽的残留：{gone}"

    css = _console_css()
    # CSS 里连注释都不该再提把手——那一整块已经换成 `.mission-order` 了。
    assert "drag-handle" not in css
    assert "cursor: grab" not in css
