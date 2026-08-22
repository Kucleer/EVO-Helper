"""军力榜的「盲滚行数」：攻击配置页上可配，**留空 = 按实测自动标定**。

口径 2026-08-22 从「屏」换成「行」：盲滚段不再慢拖，改成连拨滚轮，而滚轮没有
「屏」这个概念，拨的是格。行是唯一同时量得住慢拖和滚轮的单位。屏口径那一份
用例（`test_ranking_blind_scrolls.py`）留着钉回滚杠杆——那一列和页面上那个框都
还在，只是不再上命令行。

这份用例钉的是**四件互相制衡**的事：

1. **没有历史时用写死的默认值**（命令行上一个 `--blind-rows` 都没有）。
2. **有历史时用 `min(最近 5 次) - 余量`，取最小值而不是最近一次或平均值。**
   ⚠️ 这一条是整份里最要紧的：实测跨度约 50 行，拿最大值或平均值去设盲滚就会
   **滚过 bot 起点**，把榜首那批军力最高的 bot 整段跳过去——而采回来的数只会
   静悄悄少一截，页面上、日志里都看不出任何异常。
3. **屏版历史样本不参与行版标定。** 库里存着一整年「翻了 N 屏到达 bot 区」，前缀
   和行版一模一样、只差单位那个字。混进来的话 78 屏会被当成 78 行（真值约 647
   行），算出来的盲滚小得离谱——小值本身安全（只白花检测段那 4.6 秒/屏），但它
   是撞上的而不是算出来的。
4. **手填的值锁死**，不再自动调；负数当场拒掉，而大数一律放行（**不设上界**）。

值住在**全局攻击配置**（`military_attack_config.blind_scroll_rows`），不是
`mission_tasks.params_json`：用户指定的位置是攻击配置页，那一页存的就是全局项。
实测样本**没有自己的表**，直接从 `system_log` 里那些「翻了 N 行到达 bot 区」
反解回来——那些行本来就在库里攒着。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.missions import MissionParamError
from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import bot_area_reached_message, bot_area_reached_rows_message
from evo_helper.domain.scheduler import GAP_FILLERS, MissionKind
from evo_helper.game.ranking_ui import (
    BLIND_SCROLL_MARGIN_ROWS,
    BLIND_SCROLL_ROWS,
    BLIND_SCROLL_SAMPLES,
    FIRST_BOT_RANK,
    ROWS_PER_SCROLL,
)
from evo_helper.infrastructure.system_log import SystemLogRecord
from evo_helper.storage import models as orm
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.storage.system_log import SystemLogRepository

from .conftest import Clock, make_supervisor
from .test_line_shortage_recovery import (
    RecordingLog,
    recorded,  # noqa: F401 - fixture，被下面的用例按名字取用
)

ORIGIN = Coordinate(2, 137, 18)

#: 生产实测（2026-08-17 同一天六趟，`system_log` 记的是屏）换算成行：
#: 77 / 78 / 73 / 74 / 72 / 78 屏 × `ROWS_PER_SCROLL`(8.3)。
#: **顺序是从旧到新**，最后一个是最近的一趟。
MEASURED = (639, 647, 606, 614, 598, 647)

#: 上面那六趟对应的屏数原值。只在「屏版样本不参与行版标定」那几条里用到。
MEASURED_SCREENS = (77, 78, 73, 74, 72, 78)

#: 用最近 5 趟（`MEASURED[1:]`）标定出来的答案：`min(598) - 83`。
CALIBRATED = 515


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 8, 22, 12, 0, tzinfo=UTC))


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
    session_factory: sessionmaker[Session],
    rows: tuple[int, ...] | list[int],
    *,
    unit: str = "rows",
) -> None:
    """把「翻了 N 行（或屏）到达 bot 区」按从旧到新写进 `system_log`。

    正文由 `bot_area_reached_rows_message` 产出而不是在这里手写：读侧反解的就是它，
    两处各写一遍的话，措辞一改用例照样绿而生产上样本全失效。

    `unit="screens"` 用来种**屏版**历史，那是切口径之前库里攒的那一年——它必须
    被行版标定整条丢掉，而这件事只有真的种进去才测得出来。
    """
    say = bot_area_reached_rows_message if unit == "rows" else bot_area_reached_message
    base = datetime(2026, 8, 22, 3, 0, tzinfo=UTC)
    SystemLogRepository(session_factory).append(
        [
            SystemLogRecord(
                logged_at_utc=base + timedelta(minutes=10 * index),
                level="INFO",
                source="tools.ranking_scan",
                host="rig",
                pid=1000 + index,
                message=say(value),
            )
            for index, value in enumerate(rows)
        ]
    )


def _configure(session_factory: sessionmaker[Session], rows: int | None) -> None:
    """把攻击配置页上那个「盲滚行数」写进库。

    这里直接改 ORM 行，而不是走 `repository.replace_military_attack_tiers`：那条
    接口眼下还不收 `blind_scroll_rows`（web 那一侧在加）。等它收了，这个助手换成
    调接口即可，用例本身一个字都不用动。
    """
    with session_factory() as session:
        row = session.get(orm.MilitaryAttackConfigRow, 1)
        if row is None:
            row = orm.MilitaryAttackConfigRow(id=1)
            session.add(row)
        row.blind_scroll_rows = rows
        session.commit()


def _blind_rows_in(command: list[str]) -> int | None:
    if "--blind-rows" not in command:
        return None
    return int(command[command.index("--blind-rows") + 1])


# -- 常量本身 ------------------------------------------------------------------


def test_the_hard_coded_fallback_is_seven_hundred_rows() -> None:
    """⚠️ **断言具体数字，不是「等于那个常量」。**

    写成自反断言（`BLIND_SCROLL_ROWS == BLIND_SCROLL_ROWS`）的话，改了常量用例
    照样绿——而这个数改大就意味着没有历史数据的那几趟会滚过 bot 起点。

    700 的来历见 `game.ranking_ui.BLIND_SCROLL_ROWS`：用户口径 2026-08-22。
    """
    assert BLIND_SCROLL_ROWS == 700


def test_the_margin_is_wide_enough_for_the_measured_noise() -> None:
    """余量必须大于实测噪声跨度，否则某些趟必然滚过头。

    六次实测 598–647 行，跨度 49；余量 83 行（= 10 屏 × 8.3）宽过它。
    余量**算出来而不是写死**，好让它不能和自己的来历分岔（见那个常量的注释）。
    """
    assert BLIND_SCROLL_MARGIN_ROWS == 83
    assert BLIND_SCROLL_MARGIN_ROWS > max(MEASURED) - min(MEASURED)


def test_the_row_samples_really_are_the_screen_samples_times_the_conversion() -> None:
    """这份用例里的行样本不是编的，是那六趟屏数换算来的。

    钉住这件事，是为了让「余量宽过噪声」那条断言仍然指着**生产实测的噪声**，
    而不是我随手挑的六个数。
    """
    assert MEASURED == tuple(round(value * ROWS_PER_SCROLL) for value in MEASURED_SCREENS)


# -- 留空 + 没有历史 = 写死的默认值 --------------------------------------------


def test_an_empty_box_without_history_keeps_the_hard_coded_default(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """样本攒不够时命令行上**一个 `--blind-rows` 都不能有**。

    断言「没有这个开关」而不是「等于 700」：默认值只该有 `BLIND_SCROLL_ROWS`
    一处，调度器这边再送一个「看起来一样」的数字过去，日后调默认值就调不动了。
    """
    _only_ranking(repository)

    assert "--blind-rows" not in _launched(scheduler, launcher)


def test_history_shorter_than_the_window_still_uses_the_default(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """差一次都不算够。

    半截样本比没有样本更危险：三趟里最小的那个纯属运气，拿它去设盲滚正是
    「按最近一次定」那条已经被否掉的做法。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[: BLIND_SCROLL_SAMPLES - 1])

    assert "--blind-rows" not in _launched(scheduler, launcher)


def test_a_year_of_screen_measurements_does_not_calibrate_the_row_knob(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **屏版历史一条都不许算进来。**

    库里存着一整年「翻了 N 屏到达 bot 区」，前缀和行版一模一样、只差单位那个字。
    混进来的话「78 屏」会被当成 78 行（真值约 647 行）——量纲差 8.3 倍，而算出来
    的只是一个「小得离谱」的盲滚，不会报错。这里种满一整窗口的屏版样本，正确的
    结果是**仍然没有答案**，退回写死的默认值。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED_SCREENS, unit="screens")

    assert "--blind-rows" not in _launched(scheduler, launcher)


def test_screen_measurements_mixed_in_do_not_shift_the_calibration(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """切口径那几天库里两种正文并存：只数行版的，屏版当不存在。

    最阴的一档是**混着**——屏版把前缀匹配的额度占掉，行版样本又确实攒够了。
    那时答案必须和「只有行版」时逐字一样。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED_SCREENS, unit="screens")
    _seed_measurements(session_factory, MEASURED[1:])

    command = scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    assert _blind_rows_in(command) == CALIBRATED


# -- 留空 + 有历史 = min(最近 K 次) − 余量 -------------------------------------


def test_the_calibration_takes_the_smallest_recent_measurement(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **这一条守的是「不许滚过头」，是整份里最要紧的。**

    喂的是带噪声的真实样本（647 / 606 / 614 / 598 / 647 行）：

    * 取最小值 → 598 − 83 = **515**（正确）
    * 取最大值 → 647 − 83 = 564 —— 比最小的那一趟还多 49 行，**必定滚过 bot 起点**
    * 取平均值 → 622 − 83 = 539 —— 同样越过 598 那一趟
    * 取最近一次 → 647 − 83 = 564 —— 同上

    三个错答案都在这里被显式排除掉，免得改成其中之一还能绿。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])

    chosen = _blind_rows_in(_launched(scheduler, launcher))

    recent = MEASURED[1:]
    mean = round(sum(recent) / len(recent))

    assert chosen == CALIBRATED
    assert chosen != max(recent) - BLIND_SCROLL_MARGIN_ROWS, "取了最大值/最近一次"
    assert chosen != mean - BLIND_SCROLL_MARGIN_ROWS, "取了平均值"


def test_only_the_most_recent_measurements_count(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """窗口之外的陈年样本不参与。

    这个数随玩家增长往上漂，把很久以前那一趟（榜单短得多）算进来只会把盲滚
    永远压在一个过时的低位上——安全，但少走的那段距离由检测段接手，而检测段
    约 4.6 秒/屏，那正是这个功能本来要省掉的部分。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, (250, *MEASURED[1:]))

    command = scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    assert _blind_rows_in(command) == CALIBRATED


def test_the_calibration_never_goes_negative(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """样本比余量还小时，答案是「一行都别盲滚」，不是一个负数。"""
    _only_ranking(repository)
    _seed_measurements(session_factory, (30,) * BLIND_SCROLL_SAMPLES)

    assert _blind_rows_in(scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)) == 0


# -- 填了数就锁死 --------------------------------------------------------------


def test_a_configured_value_wins_over_the_calibration(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """手填的是**覆盖**，不是初值：有历史也照样用手填的那个数。"""
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])
    _configure(session_factory, 300)

    assert _blind_rows_in(_launched(scheduler, launcher)) == 300


def test_zero_means_no_blind_spin_at_all_and_is_not_treated_as_blank(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """`0` 是一个真的取值：一行都别盲滚，从第一屏就开始检测。

    它是**最保守**的取值（多花几屏廉价检测，绝不可能滚过头），所以必须放行。
    把它当成「留空」就等于在用户明确要求最保守时反而去滚 515 行。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])
    _configure(session_factory, 0)

    assert _blind_rows_in(scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)) == 0


def test_a_missing_config_row_is_read_as_blank_not_as_a_broken_task(  # type: ignore[no-untyped-def]
    repository, launcher, clock
) -> None:
    """配置行还没建出来（老库、或者 `ensure_mission_rows()` 还没跑）= 当成留空。

    ⚠️ **不许在这里抛 `MissionParamError`。** 那个异常的后果是**自动停用到用户
    手动恢复为止**，不只是「这一轮不跑」——为一张还没初始化的配置表把整条采集
    链路关掉一整夜，是不成比例的。
    """
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)

    command = scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    assert "--blind-rows" not in command


# -- 拒掉不可能的取值 ----------------------------------------------------------


@pytest.mark.parametrize("raw", [-1, -700])
def test_negative_values_are_refused(scheduler: MissionScheduler, raw: int) -> None:
    """负数没有意义。**只拒这一侧。**"""
    with pytest.raises(MissionParamError):
        scheduler.validate_blind_scroll_rows(raw)


@pytest.mark.parametrize("raw", [3.5, "很多", True, [700]])
def test_non_integer_values_are_refused(scheduler: MissionScheduler, raw: object) -> None:
    """`True` 也要拒：`bool` 是 `int` 的子类，放过去就成了「盲滚 1 行」。"""
    with pytest.raises(MissionParamError):
        scheduler.validate_blind_scroll_rows(raw)


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_a_blank_value_is_not_an_error_it_is_the_auto_mode(
    scheduler: MissionScheduler, raw: object
) -> None:
    assert scheduler.validate_blind_scroll_rows(raw) is None


@pytest.mark.parametrize("raw", [FIRST_BOT_RANK, FIRST_BOT_RANK + 1, 700, 5000])
def test_a_value_past_the_supposed_bot_start_is_still_accepted(
    scheduler: MissionScheduler, raw: int
) -> None:
    """⚠️ **不设上界，`FIRST_BOT_RANK`(587) 更不是边界。**

    用户口径（2026-08-22）：那个「bot 起点」是**玩家改名伪装**出来的——判据只看
    名字前缀 `bot_`，改名的真人一样命中，真 bot 区在更后面。所以默认的 700 行并
    不越界，而 `BLIND_SCROLLS` 注释里「40×12=480 < 587」那套推理的前提已经不成立。

    拿一个被伪装污染的边界报警，比不报警更坏；这里报警的代价还格外高：
    `MissionParamError` 会把任务**停用到用户手动恢复为止**。

    滚过头的代价仍然是真的（静悄悄少采一截），所以那句警告留在界面上——但它是
    **提示**，不是拦路。
    """
    assert scheduler.validate_blind_scroll_rows(raw) == raw


def test_the_screen_era_validator_still_accepts_values(scheduler: MissionScheduler) -> None:
    """屏口径那把尺子还是活的：那一列和页面上那个框留着当**回滚杠杆**。

    ⚠️ 但它写进去的值**不再上命令行**——那一条由
    `test_ranking_blind_scrolls.py` 钉着。两件事分开测，是因为「校验还收得住」
    和「取值还生效」在回滚期间恰好是一真一假。
    """
    assert scheduler.validate_blind_scrolls(40) == 40
    assert scheduler.validate_blind_scrolls("") is None


# -- 判定本身要在日志里说得出来 ------------------------------------------------
#
# ⚠️ **补的是自动标定唯一的哑点。** `bot_area_reached_rows_message` 上写着：那句
# 实测日志的措辞一改，攒下的样本一次性作废，标定就**静悄悄退回写死的默认值**
# ——页面上、日志里都看不出任何异常。采集那头照样打「盲滚 700 行」，看上去和
# 「本来就没攒够样本」一模一样。所以差别只能由判定这一侧说出来。


def _verdicts(log: RecordingLog) -> list[str]:
    """只挑盲滚那一类的日志。同一个 tick 里还会写别的（定时窗口之类）。"""
    return [message for message in log.messages if "盲滚行数" in message]


def _verdict_payloads(log: RecordingLog) -> list[dict[str, object]]:
    return [
        payload
        for message, payload in zip(log.messages, log.payloads, strict=True)
        if "盲滚行数" in message
    ]


def test_a_missing_calibration_says_so_and_counts_the_samples(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ **样本条数是「刚上线」和「反解规则失效了」唯一的分界。**

    两种情形下命令行完全一样（不带 `--blind-rows`）、采集日志也完全一样
    （「盲滚 700 行」）。区别只有一个：前者的样本会一天天涨上去，后者恒为 0。
    不把这个数写进 `payload_json`，事后就只能靠猜。

    切口径那几天还多一档：屏版样本被整条丢掉，于是这个数会先掉回 0 再重新涨
    ——那一段看起来和「反解失效」一模一样，唯一分得开的就是它在往上走。
    """
    _only_ranking(repository)

    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    assert len(_verdicts(recorded)) == 1
    assert str(BLIND_SCROLL_ROWS) in _verdicts(recorded)[0], "没说清回落到的是哪个默认值"
    payload = _verdict_payloads(recorded)[0]
    assert payload["source"] == "default"
    assert payload["blind_scroll_rows"] is None
    assert payload["measurements"] == 0
    assert payload["samples_required"] == BLIND_SCROLL_SAMPLES
    assert payload["margin"] == BLIND_SCROLL_MARGIN_ROWS


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
    assert payload["blind_scroll_rows"] == CALIBRATED
    assert payload["measurements"] == len(MEASURED[1:])
    assert str(CALIBRATED) in _verdicts(recorded)[0]


def test_a_hand_typed_value_is_reported_as_hand_typed(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """手填和标定必须分得开。

    盲滚滚过头的后果是**采回来的数静悄悄少一截**，而两种来源的善后完全不同：
    一个要去攻击配置页上改，一个要去看实测样本。日志说成同一句，用户只能挨个试。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])
    _configure(session_factory, 300)

    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    payload = _verdict_payloads(recorded)[0]
    assert payload["source"] == "manual"
    assert payload["blind_scroll_rows"] == 300
    assert "手填" in _verdicts(recorded)[0]


def test_the_verdict_is_only_written_when_it_changes(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ **限流：判定没变就一个字都不写。**

    `_blind_rows` 在每次组军力榜命令行时都会走，而 `command_for` 那条公开路径
    **页面保存配置时也会走**。每次都写的话，一天几十条重复的「还是 515 行」会把
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

    「样本攒够了、标定第一次给出答案」正是用户最该看到的那一条：在此之前每趟都
    白检测一大段，从这条起不再白花。压掉它，这个功能到底有没有生效就无从查起。
    """
    _only_ranking(repository)
    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)
    assert _verdict_payloads(recorded)[0]["source"] == "default"

    _seed_measurements(session_factory, MEASURED[1:])
    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    assert len(_verdicts(recorded)) == 2
    assert _verdict_payloads(recorded)[1]["source"] == "calibrated"
    assert _verdict_payloads(recorded)[1]["blind_scroll_rows"] == CALIBRATED


def test_the_verdict_never_claims_a_scan_actually_ran(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ **措辞只说判定，不说「这一趟滚了多少行」。**

    走到这里未必真会起一轮采集：`command_for` 是页面拿来校验参数的，组出来的
    命令行随手就丢了。说成「本趟盲滚 N 行」就是替一件没发生的事作证——而这个
    仓库今天已经为「日志说假话」付过两天的代价（`bot_loop._say_still_waiting`）。

    真正「这一趟滚了多少行」那句话在 `tools.ranking_scan` 里，由**真的滚完了**的
    那一侧打出来。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])

    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    verdict = _verdicts(recorded)[0]
    assert "判定" in verdict
    assert "本趟" not in verdict
    assert "已盲滚" not in verdict


def test_the_verdict_says_rows_not_screens(  # type: ignore[no-untyped-def]
    scheduler,
    repository,
    session_factory,
    recorded: RecordingLog,  # noqa: F811
) -> None:
    """⚠️ **单位那个字要念对。**

    515 行和 515 屏差着 8.3 倍。判定日志是排障时唯一的入口，念错单位的日志比
    没有日志更坏——照着它去推，怎么算都对不上现场。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])

    scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)

    verdict = _verdicts(recorded)[0]
    assert f"{CALIBRATED} 行" in verdict
    assert "屏" not in verdict


# -- 与采集那一侧对得上 --------------------------------------------------------


def test_the_scan_tool_takes_the_same_flag_the_scheduler_sends() -> None:
    """⚠️ **两侧的开关名必须一个字不差。**

    对不上的后果不是报错：`argparse` 不认识的参数会让 runner 立刻退出，而
    调度器看到的只是「军力榜任务又异常退出了」——连着几次就自动停用。所以这里
    直接对着源码找那个字符串，而不是等实机上撞出来。
    """
    source = (
        Path(__file__).resolve().parents[3] / "src" / "evo_helper" / "tools" / "ranking_scan.py"
    ).read_text(encoding="utf-8")

    assert '"--blind-rows"' in source, "采集那侧没有这个开关，调度器送过去会让 runner 直接退出"
