"""军力榜的「盲拖屏数」——**屏口径，现在是一键回滚那条路。**

口径 2026-08-22 换成行（滚轮连拨取代慢拖，而滚轮没有「屏」这个概念）。真正驱动
盲滚的那一份用例搬到了 `test_ranking_blind_rows.py`；这一份留下来钉的是**回滚
杠杆还在**这件事，那是两个不同的断言：

1. `military_attack_config.blind_scrolls` 那一列、攻击配置页上那个框、
   `validate_blind_scrolls` 那把尺子**都还是活的**——页面照旧要在写库之前量一遍。
2. 但那个值**不再上命令行**：调度器组的是 `--blind-rows`。
3. 屏口径的判定机器（`_blind_scrolls` 和它那三个帮手）原样留着，接回去就能用。
   ⚠️ **一条不测的回滚路不是回滚路**：等真要回滚那天才发现它烂了，就等于没有。

盲拖是「开榜之后先无脑往下拖几屏再开始逐屏检测 bot」——那一段必定还是真人，
检测纯属白花。这个数长期写死成 40，而生产实测（2026-08-17 同一天六趟）
到达 bot 区要 77 / 78 / 73 / 74 / 72 / 78 屏：中间 32–38 屏全在白检测，
按每天 8 趟算一天白花 250–300 次。那六个数在行口径下换算成行，仍然是
`test_ranking_blind_rows.py` 里标定用的样本。
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


# -- 常量本身 ------------------------------------------------------------------


def test_the_hard_coded_fallback_is_still_forty_screens() -> None:
    """⚠️ **断言具体数字，不是「等于那个常量」。**

    写成 `assert BLIND_SCROLLS == BLIND_SCROLLS` 那样的自反断言，改了常量用例
    照样绿——而这个数改大就意味着**回滚之后**那几趟会拖过 bot 起点。

    40 的来历见 `game.ranking_ui`：按推进速率的上界 12 名/屏算，40 屏最多推进
    480 名。⚠️ 那套推理里的「够不到 bot 起点 587」前提已经不成立（用户口径
    2026-08-22：587 那一段是玩家改名伪装的），但 40 这个数本身仍是回滚那条路
    唯一的默认值，采集那侧的形参默认值必须跟它对得上。
    """
    assert BLIND_SCROLLS == 40
    assert inspect.signature(scan).parameters["blind_scrolls"].default == 40


def test_the_calibration_window_and_margin_are_wide_enough_for_the_measured_noise() -> None:
    """余量必须大于实测噪声跨度，窗口必须宽过连着几趟偏大的那一段。

    六次实测 72–78，跨度 6：余量小于 6 就意味着某些趟必然拖过头。
    窗口取 5 是因为 `77 / 78 / 78` 这样连着三趟偏大的窗口在实测里真的出现过，
    K=3 的最小值就是 77——那正好落回噪声区间里。

    ⚠️ 这两个数在行口径下仍然当家：`BLIND_SCROLL_SAMPLES` 两边共用，
    `BLIND_SCROLL_MARGIN_ROWS` 是拿 `BLIND_SCROLL_MARGIN` 换算出来的。
    """
    assert BLIND_SCROLL_MARGIN == 10
    assert BLIND_SCROLL_SAMPLES == 5
    assert BLIND_SCROLL_MARGIN > max(MEASURED) - min(MEASURED)


def test_the_manual_ceiling_matches_what_the_calibration_would_pick() -> None:
    """手填的上界 = 把自动标定那道公式套在已记录的实测上。

    这样用户想把当下自动算出来的值钉死时填得进去，而手填的数又不会越过
    **有实测支撑**的那条线。（这个常量眼下只剩界面上那句提示在用，见下面
    「不设上界」那条。）
    """
    assert BLIND_SCROLLS_MAX == 62
    assert BLIND_SCROLLS_MAX == min(MEASURED) - BLIND_SCROLL_MARGIN


# -- 屏数不再上命令行 ----------------------------------------------------------


def test_the_screen_knob_no_longer_reaches_the_command_line(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher, session_factory
) -> None:
    """⚠️ **手填了屏数也不许出现 `--blind-scrolls`。**

    两个开关在 `tools.ranking_scan` 上并存是有意的（`--blind-scrolls` 就是回滚
    那条路），但**优先级要显式**：只传 `--blind-scrolls` 的意思是「退回慢拖」。
    调度器要是在正常路径上顺手把它带上，等于每一趟都在回滚——而回滚是要人
    显式选的，不该由一个存量配置值悄悄决定。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])
    repository.replace_military_attack_tiers("[]", blind_scrolls=25)

    command = _launched(scheduler, launcher)

    assert "--blind-scrolls" not in command
    assert "25" not in command


def test_the_screen_era_decision_still_works_if_it_is_wired_back(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory
) -> None:
    """⚠️ **一条不测的回滚路不是回滚路。**

    `_blind_scrolls` 眼下没有调用点（组命令行走的是 `_blind_rows`），所以这里
    刻意伸手去按那个私有方法：不按的话它会静静地烂掉，而发现的时机正是要回滚
    的那一天——那时没人有空调试判据。

    钉的是取值顺序在屏口径下**一条没变**：手填优先，留空按 `min(最近 5 次) - 10`
    自标定。
    """
    _only_ranking(repository)
    _seed_measurements(session_factory, MEASURED[1:])

    assert scheduler._blind_scrolls() == min(MEASURED[1:]) - BLIND_SCROLL_MARGIN

    repository.replace_military_attack_tiers("[]", blind_scrolls=25)

    assert scheduler._blind_scrolls() == 25


# -- 那把尺子还是活的 ----------------------------------------------------------


@pytest.mark.parametrize("raw", [-1, -40])
def test_negative_values_are_refused(scheduler: MissionScheduler, raw: int) -> None:
    """负数没有意义。**只拒这一侧。**"""
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


def test_zero_is_a_real_value_and_not_a_blank(scheduler: MissionScheduler) -> None:
    """`0` 是用户真的敲进去的「一屏都别盲拖」——**最保守**的取值，必须放行。

    把它当成「留空」就等于在用户明确要求最保守时反而去拖 62 屏。
    """
    assert scheduler.validate_blind_scrolls(0) == 0


@pytest.mark.parametrize("raw", [BLIND_SCROLLS_MAX, BLIND_SCROLLS_MAX + 1, 70, 999])
def test_a_value_above_the_recorded_minimum_is_still_accepted(
    scheduler: MissionScheduler, raw: int
) -> None:
    """⚠️ **不设上界**（用户口径 2026-08-17：「不需要这个限制」）。

    这里曾经拒掉大于 `BLIND_SCROLLS_MAX` 的值。那个数是从**已记录的最小实测屏数
    减余量**推出来的——它只反映我们碰巧量到过什么，不是游戏的事实。榜会随玩家
    增加变长，实测值也在涨，把一个观测下界当成硬闸门，结果就是用户明明知道该
    填 70 却填不进去。

    调大的代价仍然是真的（拖过 bot 起点会静悄悄少采一截），所以那句警告留在
    界面上——但它是**提示**，不是拦路。
    """
    assert scheduler.validate_blind_scrolls(raw) == raw


# -- 页面上说得出这件事 --------------------------------------------------------


def test_the_attack_settings_page_carries_the_box_and_the_warning() -> None:
    """框要在**攻击配置页**上（用户指定的位置），而且旁边写清风险。

    一个光秃秃的数字框自己说不出后果。而这个值填错了的后果恰恰是
    不报错、不少日志、只是数少一截——页面上不写明白，用户没有别的途径知道。

    ⚠️ 这个框在行口径下**留着不删**（设计口径 2026-08-22：不删列、不删页面项），
    它是回滚杠杆的用户入口。但它**当前不生效**，页面必须说出这件事——
    否则用户会一直调一个根本不上命令行的数。

    ⚠️ **这里刻意不再断言「宁小勿大」和「拖过 bot 起点」那两句原文。**
    - 「拖过 bot 起点」建立在 `FIRST_BOT_RANK`(587) 是真 bot 边界之上，而用户口径
      （2026-08-22）说那一段是**玩家改名伪装**的 bot，判据只看名字前缀，改名的真人
      一样命中。前提不成立，页面上继续那么写就是拿假边界吓用户。
    - 「宁小勿大」在**行口径下正好反过来**：盲滚少走的行由检测段接手，而检测段每屏
      要做一次整列 OCR（约 4.6 秒/屏），比多拨几格贵得多——所以行那个框写的是
      「宁可多滚」。两个框的取向不同，断言不能共用一句话。

    钉的是**实质**而不是措辞：框还在、留空的含义写着、静默漏采的后果写着、
    以及「这个框当前不生效」。
    """
    page = (Path(evo_helper.web.__file__).parent / "templates" / "settings.html").read_text(
        encoding="utf-8"
    )

    assert 'id="blind-scrolls"' in page, "攻击配置页上没有这个输入框，等于功能不存在"
    assert "留空 = 按实测自动标定" in page
    assert "当前不生效" in page, "这个框已经不上命令行了，页面必须说出来"
    assert "少一截" in page, "静默漏采这个后果必须写在页面上"
    assert "拖过 bot 起点" not in page, "587 不是真 bot 边界，这句假前提不许留在页面上"
