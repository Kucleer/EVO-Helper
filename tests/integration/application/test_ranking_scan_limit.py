"""军力榜的「扫描数量范围」：配了就是这一趟的上限，**留空就是全扫**。

用户口径（2026-08-17）：「军力扫描增加扫描数量范围，为空则全扫」。

这个文件钉的是**两件互相制衡**的事，缺一件另一件就会走样：

1. **留空 = 全扫。** 这是加这个框之前就有的行为，也是绝大多数时候想要的：
   榜单一趟翻到底才写得全。空框在页面上根本不往上送（`missions.html` 里
   `.mission-param` 的处理器只送非空的），所以 `params_json` 里没有这个键；
   一旦有人把「没配」当成 0 或者当成某个默认值，采集就会在第 N 个 bot 上
   收工，而页面上什么都看不出来——库里少的那几千个 bot 不会报错。
2. **配了 N 就最多 N 个。** 否则这个框是个摆设，用户填完以为限住了，
   实际上照样跑一个多小时。

参数走 `mission_tasks.params_json`，**没有为它加数据库列**（同 `galaxy` /
`first_system` 那几个，见 `storage.models` 上那一行的注释）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import evo_helper.web
from evo_helper.application.mission_scheduler import MissionScheduler
from evo_helper.domain.missions import MissionParamError
from evo_helper.domain.models import Coordinate
from evo_helper.domain.scheduler import GAP_FILLERS, MissionKind
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.persistent_service import ranking_scan_summary

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import set_score_window

ORIGIN = Coordinate(2, 137, 18)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


@pytest.fixture(autouse=True)
def military_window(repository) -> None:  # type: ignore[no-untyped-def]
    """本模块的选靶窗口基线，摆在**全局**攻击配置里：有效期 2 小时、窗口门限 50 个。

    2026-08-23 起有效期与窗口门限是全局的（`military_attack_config`），不再是任务
    参数——从前它们就写在上面那串 JSON 里，一眼看得见。搬走之后若不摆，每条用例吃的
    都是代码默认值（2 小时 / **100 个**），而这个模块的候选池只有两三个目标：门限 100
    会让每一条用例都走「放弃窗口」那一支，于是本该量到的东西量不到，而用例照样是绿的。
    """
    set_score_window(repository, max_age_hours=2, window_floor=50)


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 8, 17, 12, 0, tzinfo=UTC))


def _task_id(repository: SqlAlchemyRepository, kind: MissionKind) -> int:
    return next(row.id for row in repository.mission_tasks() if row.kind == kind.value)


def _only_ranking(repository: SqlAlchemyRepository, params_json: str) -> None:
    """只留军力榜这一条能跑，并给它配上这份参数。

    另一条填空隙的（扫描）必须关掉：不关的话它会顶上来把空隙填掉，
    于是断言看到的是扫描那条命令行，而不是军力榜的。
    """
    for kind in GAP_FILLERS:
        if kind is not MissionKind.RANKING:
            repository.update_mission_task(_task_id(repository, kind), enabled=False)
    repository.update_mission_task(
        _task_id(repository, MissionKind.RANKING), enabled=True, params_json=params_json
    )


def _launched(scheduler: MissionScheduler, launcher) -> list[str]:  # type: ignore[no-untyped-def]
    scheduler.start()
    scheduler.tick()
    assert launcher.kinds == [MissionKind.RANKING]
    return list(launcher.latest.command)


# -- 留空 = 全扫 ---------------------------------------------------------------


def test_an_empty_scan_count_scans_the_whole_ranking(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """没配数量时，起出来的命令行上**一个 `--bot-limit` 都不能有**。

    断言「没有这个开关」而不是「限额等于某个大数」：`ranking_scan.scan()` 判的是
    `bot_limit is not None`，塞一个「足够大」的数进去在类型上是合法的、在行为上
    也几乎看不出来，但它会把「全扫」悄悄变成「扫到某个数为止」。
    """
    _only_ranking(repository, "{}")

    command = _launched(scheduler, launcher)

    assert "--bot-limit" not in command
    assert command[1:] == ["-u", "-m", "evo_helper.tools.ranking_scan"]


def test_a_blank_scan_count_is_the_same_as_not_configuring_it(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """空串也是「没配」。页面正常不会送它上来，但库里可以有旧值或手改的值。"""
    _only_ranking(repository, '{"bot_limit": ""}')

    assert "--bot-limit" not in _launched(scheduler, launcher)


# -- 配了 N 就最多 N 个 --------------------------------------------------------


def test_a_configured_scan_count_caps_this_run(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    _only_ranking(repository, '{"bot_limit": 30}')

    assert _launched(scheduler, launcher)[1:] == [
        "-u",
        "-m",
        "evo_helper.tools.ranking_scan",
        "--bot-limit",
        "30",
    ]


def _wants_military_targets(repository) -> None:  # type: ignore[no-untyped-def]
    """开一条「按军力选靶」的 bot 任务 —— 有它在等，这一趟榜单才算「有批次」。

    没有它时 `_military_batch_task()` 交 `None`，「窗口门限」那条默认路根本不该走：
    没有任何军力任务在等这批目标，拿一个攻击侧的数去卡扫描链路是没有道理的。
    """
    repository.update_mission_task(
        _task_id(repository, MissionKind.BOT),
        enabled=True,
        params_json='{"by_military": true}',
    )


def test_a_configured_scan_count_wins_over_the_window_floor(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """⚠️⚠️ **用户填了「扫描数量」就听用户的，「窗口门限」不许把它压小。**

    页面上那两个数说的是不同的事：

        扫描数量（任务中心 · 扫描军力榜那一行）   用户给扫描链路划的上限
        窗口门限（攻击配置 · 军力选靶窗口）       攻击那边至少要看到多少个

    这里曾经取两者的 `min`，理由写着「取大的会越过用户划的线，取任务那个又会让
    批次采不满」。**那是反的**：用户填「扫描数量 700、窗口门限 500」时，`min` 把
    批次压成 500 —— 而 500 正是攻击要达到的数，扣掉「24h 内已打」之类的排除项之后
    **必然低于门限**。于是它恰好造成了自己想避免的「批次采不满」，而且是结构上
    永远不可能满。

    2026-08-25 生产实测：一趟写入 500 条、窗口内 396–468、门限 500，连着十几趟都
    够不着；让位判据每轮白等 3 分钟耐心才回落到照旧打，攻击一直在用旧读数。

    700 既没越过用户划的线，又超额满足了门限 —— 这才是两条都守住。

    ⚠️ 构造成 700 > 500，两个数**必须朝这个方向**差开：老用例填的是 3、门限 50，
    `min` 与「听用户的」给出同一个答案，于是它绿着看了一整周，什么都没钉住。
    """
    _only_ranking(repository, '{"bot_limit": 700}')
    _wants_military_targets(repository)
    set_score_window(repository, window_floor=500)

    assert _launched(scheduler, launcher)[-2:] == ["--bot-limit", "700"]


def test_a_scan_count_below_the_floor_is_also_honoured(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """⚠️ 填得**比门限小**时同样听用户的 —— 那条线是他自己划的。

    这一条守的是反方向：别好心替用户放大。够不够是另一回事，日志里那句
    「不再让位（门限可能配得比榜上能采到的还高）」会把它说出来。
    """
    _only_ranking(repository, '{"bot_limit": 20}')
    _wants_military_targets(repository)
    set_score_window(repository, window_floor=500)

    assert _launched(scheduler, launcher)[-2:] == ["--bot-limit", "20"]


def test_the_window_floor_is_the_default_only_when_nothing_was_configured(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """留空时「窗口门限」当默认值 —— 那时它表达的是「这一批攻击至少要这么多」。

    ⚠️ 这一条和上面两条是一对：少了它，「拿掉 `min`」会退化成「门限在这里彻底没用了」，
    于是留空那条路变成全扫（一趟几百屏），把攻击饿死。
    """
    _only_ranking(repository, "{}")
    _wants_military_targets(repository)
    set_score_window(repository, window_floor=500)

    assert _launched(scheduler, launcher)[-2:] == ["--bot-limit", "500"]


def test_no_waiting_military_task_means_no_floor_is_imposed(  # type: ignore[no-untyped-def]
    scheduler, repository, launcher
) -> None:
    """⚠️ 没有军力任务在等这批目标时，留空还是**全扫**，不许套门限。

    「窗口门限」是攻击侧的数。一条军力任务都没在等的时候拿它去卡扫描链路，
    等于凭空给「留空 = 全扫」加了个上限，而页面上看不出来。
    """
    _only_ranking(repository, "{}")
    set_score_window(repository, window_floor=500)

    assert "--bot-limit" not in _launched(scheduler, launcher)


# -- 拒掉不可能的取值 ----------------------------------------------------------


@pytest.mark.parametrize("raw", ['{"bot_limit": 0}', '{"bot_limit": -5}'])
def test_zero_or_negative_is_refused_instead_of_silently_scanning_nothing(  # type: ignore[no-untyped-def]
    scheduler, raw: str
) -> None:
    """`0` 不是「全扫」，也不是一个能跑的数量：它的意思只能是「别跑」。

    要停掉这条链路有复选框。让 `0` 通过等于起一个必然什么都不采的采集，
    而页面上它看起来一切正常。
    """
    with pytest.raises(MissionParamError):
        scheduler.command_for(MissionKind.RANKING, raw, origin=ORIGIN)


def test_a_non_numeric_scan_count_is_refused(scheduler: MissionScheduler) -> None:
    with pytest.raises(MissionParamError):
        scheduler.command_for(MissionKind.RANKING, '{"bot_limit": "很多"}', origin=ORIGIN)


# -- 页面上说得出这件事 --------------------------------------------------------


def test_the_console_row_says_that_an_empty_box_means_a_full_scan() -> None:
    """「留空 = 全扫」必须**写在页面上**，不能只写在这份文档里。

    一个空的数字框自己说不出它是什么意思：可能是「还没配」，也可能是
    「配了但没生效」。回显那一句就是用来消掉这个歧义的。
    """
    assert "留空 = 全扫" in ranking_scan_summary({})
    assert "留空 = 全扫" in ranking_scan_summary({"bot_limit": ""})
    # 2026-08-20 起这句回显后面还跟着「扫描间隔」那一格的话（同一行两个框，
    # 一句回显把两件事都说了），所以量的是开头而不是整句。间隔那一半由
    # `test_ranking_scan_cooldown.py` 自己量。
    assert ranking_scan_summary({"bot_limit": 30}).startswith("本趟最多采 30 个 bot（留空 = 全扫）")


def test_the_missions_page_gives_the_ranking_row_a_count_box() -> None:
    """调度台上军力榜那一行要有这个输入框，而且旁边写着「留空 = 全扫」。

    参数只在库里能存、页面上没地方填，等于这个功能不存在。
    """
    page = (Path(evo_helper.web.__file__).parent / "templates" / "missions.html").read_text(
        encoding="utf-8"
    )

    assert "RANKING: [" in page, "PARAM_FIELDS 里没有军力榜那一条，页面上就不会有输入框"
    assert "key: 'bot_limit'" in page
    assert "留空 = 全扫" in page


def test_the_console_validates_the_count_with_the_same_ruler_as_the_launcher(
    scheduler: MissionScheduler,
) -> None:
    """页面保存前那道校验走的就是 `command_for`（见 `web.persistent_service._validate`）。

    这条把「能存下来的」和「起得来的」钉成同一件事：两边分家的结果是页面收下了、
    调度器起不来，而起不来时它只会把任务自动停用，用户要等下次看页面才发现。
    """
    assert "--bot-limit" not in scheduler.command_for(MissionKind.RANKING, "{}", origin=ORIGIN)
    assert scheduler.command_for(MissionKind.RANKING, '{"bot_limit": 7}', origin=ORIGIN)[-2:] == [
        "--bot-limit",
        "7",
    ]
