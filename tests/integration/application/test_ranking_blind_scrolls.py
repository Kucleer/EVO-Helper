"""军力榜的「盲拖屏数」：攻击配置页上可配，**留空 = 按实测自动标定**。

用户口径（2026-08-17）：「盲拖数量需在攻击配置页可配置」。

盲拖是「开榜之后先无脑往下拖几屏再开始逐屏检测 bot」——那一段必定还是真人，
检测纯属白花。这个数长期写死成 40，而生产实测（2026-08-17 同一天六趟）
到达 bot 区要 77 / 78 / 73 / 74 / 72 / 78 屏：中间 32–38 屏全在白检测，
按每天 8 趟算一天白花 250–300 次。

这份用例钉的是**三件互相制衡**的事：

1. **没有历史时用写死的默认值 40。** 这是加这个功能之前的行为。
2. **有历史时用 `min(最近 5 次) - 10`，取最小值而不是最近一次或平均值。**
   ⚠️ 这一条是整份里最要紧的：六次实测跨度 6 屏（72–78），拿 78 或者平均值去设
   盲拖就会**拖过 bot 起点**，把榜首那批军力最高的 bot 整段跳过去——而采回来的
   数只会静悄悄少一截，页面上、日志里都看不出任何异常。
3. **手填的值锁死**，不再自动调；不可能的取值当场拒掉。

值住在**全局攻击配置**（`military_attack_config.blind_scrolls`），不是
`mission_tasks.params_json`：用户指定的位置是攻击配置页，那一页存的就是全局项。
实测样本**没有自己的表**，直接从 `system_log` 里那些「翻了 N 屏到达 bot 区」
反解回来——那些行本来就在库里攒着。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

import evo_helper.web
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.missions import MissionParamError
from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import bot_area_reached_message
from evo_helper.domain.scheduler import GAP_FILLERS, MissionKind
from evo_helper.game.ranking_ui import (
    BLIND_SCROLL_MARGIN,
    BLIND_SCROLL_SAMPLES,
    BLIND_SCROLLS,
    BLIND_SCROLLS_MAX,
)
from evo_helper.infrastructure.system_log import SystemLogRecord
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.storage.system_log import SystemLogRepository
from evo_helper.tools.ranking_scan import scan

from .conftest import Clock, make_supervisor
from .test_line_shortage_recovery import (
    RecordingLog,
    recorded,  # noqa: F401 - fixture，被下面的用例按名字取用
)

ORIGIN = Coordinate(2, 137, 18)

#: 生产实测（2026-08-17 同一天六趟，`system_log`）：到达 bot 区用了这么多屏。
#: **顺序是从旧到新**，最后一个是最近的一趟。
MEASURED = (77, 78, 73, 74, 72, 78)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 8, 17, 12, 0, tzinfo=UTC))


def _task_id(repository: SqlAlchemyRepository, kind: MissionKind) -> int:
    return next(row.id for row in repository.mission_tasks() if row.kind == kind.value)


def _only_ranking(repository: SqlAlchemyRepository) -> None:
    """只留军力榜这一条能跑。

    另一条填空隙的（扫描）必须关掉：不关的话它会顶上来把空隙填掉，
    于是断言看到的是扫描那条命令行，而不是军力榜的。
    """
    for kind in GAP_FILLERS:
        if kind is not MissionKind.RANKING:
            repository.update_mission_task(_task_id(repository, kind), enabled=False)
    repository.update_mission_task(_task_id(repository, MissionKind.RANKING), enabled=True)


def _launched(scheduler: MissionScheduler, launcher) -> list[str]:  # type: ignore[no-untyped-def]
    scheduler.start()
    scheduler.tick()
    assert launcher.kinds == [MissionKind.RANKING]
    return list(launcher.latest.command)


def _seed_measurements(
    session_factory: sessionmaker[Session], scrolls: tuple[int, ...] | list[int]
) -> None:
    """把「翻了 N 屏到达 bot 区」按从旧到新写进 `system_log`。

    正文由 `bot_area_reached_message` 产出而不是在这里手写：读侧反解的就是它，
    两处各写一遍的话，措辞一改用例照样绿而生产上样本全失效。
    """
    base = datetime(2026, 8, 17, 3, 0, tzinfo=UTC)
    SystemLogRepository(session_factory).append(
        [
            SystemLogRecord(
                logged_at_utc=base + timedelta(minutes=10 * index),
                level="INFO",
                source="tools.ranking_scan",
                host="rig",
                pid=1000 + index,
                message=bot_area_reached_message(value),
            )
            for index, value in enumerate(scrolls)
        ]
    )


def _blind_scrolls_in(command: list[str]) -> int | None:
    if "--blind-scrolls" not in command:
        return None
    return int(command[command.index("--blind-scrolls") + 1])


# -- 常量本身 ------------------------------------------------------------------


def test_the_hard_coded_fallback_is_still_forty_screens() -> None:
    """⚠️ **断言具体数字，不是「等于那个常量」。**

    写成 `assert BLIND_SCROLLS == BLIND_SCROLLS` 那样的自反断言，改了常量用例
    照样绿——而这个数改大就意味着没有历史数据的那几趟会拖过 bot 起点。

    40 的来历见 `game.ranking_ui`：按推进速率的上界 12 名/屏算，40 屏最多推进
    480 名，够不到 bot 起点 587。
    """
    assert BLIND_SCROLLS == 40
    assert inspect.signature(scan).parameters["blind_scrolls"].default == 40


def test_the_calibration_window_and_margin_are_wide_enough_for_the_measured_noise() -> None:
    """余量必须大于实测噪声跨度，窗口必须宽过连着几趟偏大的那一段。

    六次实测 72–78，跨度 6：余量小于 6 就意味着某些趟必然拖过头。
    窗口取 5 是因为 `77 / 78 / 78` 这样连着三趟偏大的窗口在实测里真的出现过，
    K=3 的最小值就是 77——那正好落回噪声区间里。
    """
    assert BLIND_SCROLL_MARGIN == 10
    assert BLIND_SCROLL_SAMPLES == 5
    assert BLIND_SCROLL_MARGIN > max(MEASURED) - min(MEASURED)


def test_the_manual_ceiling_matches_what_the_calibration_would_pick() -> None:
    """手填的上界 = 把自动标定那道公式套在已记录的实测上。

    这样用户想把当下自动算出来的值钉死时填得进去，而手填的数又不会越过
    **有实测支撑**的那条线。
    """
    assert BLIND_SCROLLS_MAX == 62
    assert BLIND_SCROLLS_MAX == min(MEASURED) - BLIND_SCROLL_MARGIN


# -- 留空 + 没有历史 = 写死的默认值 --------------------------------------------


def test_an_empty_box_without_history_keeps_the_hard_coded_default(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """样本攒不够时命令行上**一个 `--blind-scrolls` 都不能有**。

    断言「没有这个开关」而不是「等于 40」：默认值只该有 `BLIND_SCROLLS` 一处，
    调度器这边再送一个「看起来一样」的数字过去，日后调默认值就调不动了。
    """
    _only_ranking(repository)

    command = _launched(scheduler, launcher)

    assert "--blind-scrolls" not in command


def test_history_shorter_than_the_window_still_uses_the_default(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """差一次都不算够。

    半截样本比没有样本更危险：三趟里最小的那个纯属运气，拿它去设盲拖正是
    「按最近一次定」那条已经被否掉的做法。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[: BLIND_SCROLL_SAMPLES - 1])

    assert "--blind-scrolls" not in _launched(scheduler, launcher)


# -- 留空 + 有历史 = min(最近 K 次) − 余量 -------------------------------------


def test_the_calibration_takes_the_smallest_recent_measurement(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **这一条守的是「不许拖过头」，是整份里最要紧的。**

    喂的是带噪声的真实样本（78 / 73 / 74 / 72 / 78）：

    * 取最小值 → 72 − 10 = **62**（正确）
    * 取最大值 → 78 − 10 = 68 —— 比最小的那一趟还多 6 屏，**必定拖过 bot 起点**
    * 取平均值 → 75 − 10 = 65 —— 同样越过 72 那一趟
    * 取最近一次 → 78 − 10 = 68 —— 同上

    三个错答案都在这里被显式排除掉，免得改成其中之一还能绿。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])

    chosen = _blind_scrolls_in(_launched(scheduler, launcher))

    recent = MEASURED[1:]
    mean = round(sum(recent) / len(recent))

    assert chosen == 62
    assert chosen != max(recent) - BLIND_SCROLL_MARGIN, "取了最大值/最近一次"
    assert chosen != mean - BLIND_SCROLL_MARGIN, "取了平均值"


def test_only_the_most_recent_measurements_count(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """窗口之外的陈年样本不参与。

    这个数随玩家增长往上漂，把很久以前那一趟（榜单短得多）算进来只会把盲拖
    永远压在一个过时的低位上——安全，但等于白花几十屏检测，也就是这个功能
    本来要省掉的那部分。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, (30, *MEASURED[1:]))

    command = scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    assert _blind_scrolls_in(command) == 62


def test_the_calibration_never_goes_negative(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """样本比余量还小时，答案是「一屏都别盲拖」，不是一个负数。"""
    _only_ranking(repository)
    _seed_measurements(session_factory, (3,) * BLIND_SCROLL_SAMPLES)

    assert _blind_scrolls_in(scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)) == 0


# -- 填了数就锁死 --------------------------------------------------------------


def test_a_configured_value_wins_over_the_calibration(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """手填的是**覆盖**，不是初值：有历史也照样用手填的那个数。"""
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])
    repository.replace_military_attack_tiers("[]", blind_scrolls=25)

    assert _blind_scrolls_in(_launched(scheduler, launcher)) == 25


def test_zero_means_no_blind_drag_at_all_and_is_not_treated_as_blank(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """`0` 是一个真的取值：一屏都别盲拖，从第一屏就开始检测。

    它是**最保守**的取值（多花几十次廉价检测，绝不可能拖过头），所以必须放行。
    把它当成「留空」就等于在用户明确要求最保守时反而去拖 62 屏。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])
    repository.replace_military_attack_tiers("[]", blind_scrolls=0)

    assert _blind_scrolls_in(scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)) == 0


# -- 拒掉不可能的取值 ----------------------------------------------------------


@pytest.mark.parametrize("raw", [-1, -40, BLIND_SCROLLS_MAX + 1, 999])
def test_out_of_range_values_are_refused(scheduler: MissionScheduler, raw: int) -> None:
    """负数没有意义；超上界的那一侧会**静悄悄少采一截**，所以只能当场拒。"""
    with pytest.raises(MissionParamError):
        scheduler.validate_blind_scrolls(raw)


@pytest.mark.parametrize("raw", [3.5, "很多", True, [40]])
def test_non_integer_values_are_refused(scheduler: MissionScheduler, raw: object) -> None:
    """`True` 也要拒：`bool` 是 `int` 的子类，放过去就成了「盲拖 1 屏」。"""
    with pytest.raises(MissionParamError):
        scheduler.validate_blind_scrolls(raw)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_a_blank_value_is_not_an_error_it_is_the_auto_mode(
    scheduler: MissionScheduler, raw: object
) -> None:
    assert scheduler.validate_blind_scrolls(raw) is None


def test_the_ceiling_itself_is_still_accepted(scheduler: MissionScheduler) -> None:
    assert scheduler.validate_blind_scrolls(BLIND_SCROLLS_MAX) == BLIND_SCROLLS_MAX


# -- 判定本身要在日志里说得出来 ------------------------------------------------
#
# ⚠️ **补的是自动标定唯一的哑点。** `bot_area_reached_message` 上写着：那句实测
# 日志的措辞一改，库里全部历史样本一次性作废，标定就**静悄悄退回写死的默认值**
# ——页面上、日志里都看不出任何异常。采集那头照样打「盲拖 40 屏」，看上去和
# 「本来就没攒够样本」一模一样。所以差别只能由判定这一侧说出来。


def _verdicts(log: RecordingLog) -> list[str]:
    """只挑盲拖那一类的日志。同一个 tick 里还会写别的（定时窗口之类）。"""
    return [message for message in log.messages if "盲拖屏数" in message]


def _verdict_payloads(log: RecordingLog) -> list[dict[str, object]]:
    return [
        payload
        for message, payload in zip(log.messages, log.payloads, strict=True)
        if "盲拖屏数" in message
    ]


def test_a_missing_calibration_says_so_and_counts_the_samples(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ **样本条数是「刚上线」和「反解规则失效了」唯一的分界。**

    两种情形下命令行完全一样（不带 `--blind-scrolls`）、采集日志也完全一样
    （「盲拖 40 屏」）。区别只有一个：前者的样本会一天天涨上去，后者恒为 0。
    不把这个数写进 `payload_json`，事后就只能靠猜。
    """
    _only_ranking(repository)

    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    assert len(_verdicts(recorded)) == 1
    assert str(BLIND_SCROLLS) in _verdicts(recorded)[0], "没说清回落到的是哪个默认值"
    payload = _verdict_payloads(recorded)[0]
    assert payload["source"] == "default"
    assert payload["blind_scrolls"] is None
    assert payload["measurements"] == 0
    assert payload["samples_required"] == BLIND_SCROLL_SAMPLES


def test_a_successful_calibration_is_written_with_its_sample_count(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """标定成功也要写：那是「这条反馈回路还活着」唯一的证据。"""
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])

    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    payload = _verdict_payloads(recorded)[0]
    assert payload["source"] == "calibrated"
    assert payload["blind_scrolls"] == 62
    assert payload["measurements"] == len(MEASURED[1:])
    assert "62" in _verdicts(recorded)[0]


def test_a_hand_typed_value_is_reported_as_hand_typed(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """手填和标定必须分得开。

    盲拖拖过头的后果是**采回来的数静悄悄少一截**，而两种来源的善后完全不同：
    一个要去攻击配置页上改，一个要去看实测样本。日志说成同一句，用户只能挨个试。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])
    repository.replace_military_attack_tiers("[]", blind_scrolls=25)

    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    payload = _verdict_payloads(recorded)[0]
    assert payload["source"] == "manual"
    assert payload["blind_scrolls"] == 25
    assert "手填" in _verdicts(recorded)[0]


def test_the_verdict_is_only_written_when_it_changes(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ **限流：判定没变就一个字都不写。**

    `_blind_scrolls` 在每次组军力榜命令行时都会走，而 `command_for` 那条公开路径
    **页面保存配置时也会走**。每次都写的话，一天几十条重复的「还是 62 屏」会把
    真正的那一次变化埋掉——而这条日志存在的全部意义就是那一次变化。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])

    for _ in range(5):
        scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    assert len(_verdicts(recorded)) == 1


def test_a_changed_verdict_is_written_again(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """限流不许压掉**真的**变化。

    「样本攒够了、标定第一次给出答案」正是用户最该看到的那一条：在此之前每趟
    都白拖三十几屏，从这条起不再白拖。压掉它，这个功能到底有没有生效就无从查起。
    """
    _only_ranking(repository)
    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)
    assert _verdict_payloads(recorded)[0]["source"] == "default"

    _seed_measurements(session_factory, MEASURED[1:])
    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    assert len(_verdicts(recorded)) == 2
    assert _verdict_payloads(recorded)[1]["source"] == "calibrated"
    assert _verdict_payloads(recorded)[1]["blind_scrolls"] == 62


def test_the_verdict_never_claims_a_scan_actually_ran(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ **措辞只说判定，不说「这一趟拖了几屏」。**

    走到这里未必真会起一轮采集：`command_for` 是页面拿来校验参数的，组出来的
    命令行随手就丢了。说成「本趟盲拖 N 屏」就是替一件没发生的事作证——而这个
    仓库今天已经为「日志说假话」付过两天的代价（`bot_loop._say_still_waiting`）。

    真正「这一趟拖了几屏」那句话在 `tools.ranking_scan` 里，由**真的拖完了**的
    那一侧打出来。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])

    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    verdict = _verdicts(recorded)[0]
    assert "判定" in verdict
    assert "本趟" not in verdict
    assert "已盲拖" not in verdict


# -- 页面上说得出这件事 --------------------------------------------------------


def test_the_attack_settings_page_carries_the_box_and_the_warning() -> None:
    """框要在**攻击配置页**上（用户指定的位置），而且旁边写清风险。

    一个光秃秃的数字框自己说不出「宁小勿大」。而这个值填大了的后果恰恰是
    不报错、不少日志、只是数少一截——页面上不写明白，用户没有别的途径知道。
    """
    page = (Path(evo_helper.web.__file__).parent / "templates" / "settings.html").read_text(
        encoding="utf-8"
    )

    assert 'id="blind-scrolls"' in page, "攻击配置页上没有这个输入框，等于功能不存在"
    assert "留空 = 按实测自动标定" in page
    assert "宁小勿大" in page
    assert "拖过 bot 起点" in page
    assert "少一截" in page
