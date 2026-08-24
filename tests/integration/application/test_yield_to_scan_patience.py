"""让位补货那道**防死锁闸**：窗口内数量停滞够久就不再让位。

判据本体（`domain.scheduler.yields_to_a_scan`）只消费一个布尔；算这个布尔的记账在
`application.mission_scheduler._scan_can_still_help`，因为只有那里有相邻两趟的历史。
这个模块钉的就是那段记账。

⚠️ **这是整条链上唯一防止静默停摆的东西。** 门限配得比榜上能采到的还高时
（生产 2026-08-24 差点这样：门限 200，而本周期总共才采到 227 个），扫描每趟都跑、
池子每趟都不涨。少了这道闸，BOT 会永远让位、一发不打，而页面显示的是「没活干」
——一句听起来正常、实际相反的话。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.application.mission_scheduler import REPEATED_LOG_WINDOW, MissionScheduler
from evo_helper.domain.scheduler import SCAN_YIELD_PATIENCE, MilitaryWindowPool, has_work

from .conftest import Clock, make_supervisor
from .test_mission_scheduler import set_score_window
from .test_multi_origin_lines import FIRST, target_near, with_lines

NOW = datetime(2026, 8, 24, 8, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> Clock:
    return Clock(NOW)


@pytest.fixture
def scheduler(repository, launcher, clock) -> MissionScheduler:  # type: ignore[no-untyped-def]
    """本模块大多数用例只问 `_scan_can_still_help` 那段记账（涨没涨、停滞多久），
    不碰库也不碰 supervisor；但最后那条要从真实的 `_facts` 走，需要配置行。
    所以照常 `prepare()`。"""
    scheduler = MissionScheduler(repository, make_supervisor(launcher, clock), clock=clock)
    scheduler.prepare()
    return scheduler


def _pool(in_window: int, floor: int = 100) -> MilitaryWindowPool:
    return MilitaryWindowPool(in_window=in_window, floor=floor)


class RecordingLog:
    """把 `record_system_log` 的调用记下来。签名与真的那一个一致。"""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, level, source, message, *, payload=None, logged_at_utc=None, **_):  # type: ignore[no-untyped-def]
        self.entries.append((level, message, dict(payload or {})))

    @property
    def yielding(self) -> list[tuple[str, str, dict[str, object]]]:
        """「让给军力榜去补货」那条 INFO。"""
        return [item for item in self.entries if item[1].startswith("窗口内只剩")]

    @property
    def gave_up(self) -> list[tuple[str, str, dict[str, object]]]:
        """「不再让位」那条 WARNING。"""
        return [item for item in self.entries if item[1].startswith("让位 ")]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> RecordingLog:
    log = RecordingLog()
    monkeypatch.setattr(
        "evo_helper.application.mission_scheduler.record_system_log", log, raising=True
    )
    return log


def _tick_until(scheduler, task_id: int, pool: MilitaryWindowPool, moments) -> None:  # type: ignore[no-untyped-def]
    for moment in moments:
        scheduler._scan_can_still_help(task_id, pool, moment)  # noqa: SLF001


def test_a_growing_pool_keeps_the_slice_with_the_scan(scheduler) -> None:  # type: ignore[no-untyped-def]
    """池子还在涨就一直让位——「采集够了就开始攻击」的前半句。

    池子是**逐屏写库**的（采集日志「逐屏写入 N 条」），所以扫描一开跑这个数就往上
    爬。判据拿涨势当「补得进来」的凭据，就不必去查「上一趟扫描几点跑的」。
    """
    assert scheduler._scan_can_still_help(1, _pool(40), NOW) is True
    assert scheduler._scan_can_still_help(1, _pool(60), NOW + timedelta(seconds=5)) is True
    assert scheduler._scan_can_still_help(1, _pool(85), NOW + timedelta(seconds=10)) is True


def test_a_pool_that_reached_the_floor_stops_yielding(scheduler) -> None:  # type: ignore[no-untyped-def]
    """采够了就不再让位，回去打——「采集够了就开始攻击」的后半句。"""
    assert scheduler._scan_can_still_help(1, _pool(40), NOW) is True

    assert scheduler._scan_can_still_help(1, _pool(100), NOW + timedelta(seconds=5)) is False


def test_a_stalled_pool_is_given_time_before_it_is_judged(scheduler) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **停滞的头几分钟不算「补不进来」。**

    军力榜落下第一行之前要先开榜、盲滚、检测 bot 区，实测约 50 秒；这段时间窗口内
    的数一动不动。判早了就会在扫描还没落第一行时判它没用，于是让位从来不生效
    ——这条口径整个白做。
    """
    assert scheduler._scan_can_still_help(1, _pool(40), NOW) is True

    # 数量一直停在 40：还在耐心之内，继续让位。
    for seconds in (5, 30, 60, 120):
        moment = NOW + timedelta(seconds=seconds)
        assert scheduler._scan_can_still_help(1, _pool(40), moment) is True, seconds


def test_a_pool_that_never_grows_eventually_gives_up(scheduler) -> None:  # type: ignore[no-untyped-def]
    """⚠️⚠️ **耐心用尽就不再让位** —— 少了这一条就是静默停摆。

    构造的正是「门限配得比榜上能采到的还高」那个形状：数量停在 40、门限 100，
    扫描再跑也到不了。
    """
    assert scheduler._scan_can_still_help(1, _pool(40), NOW) is True
    assert scheduler._scan_can_still_help(1, _pool(40), NOW + timedelta(seconds=5)) is True

    past = NOW + timedelta(seconds=5) + SCAN_YIELD_PATIENCE + timedelta(seconds=1)

    assert scheduler._scan_can_still_help(1, _pool(40), past) is False


def test_the_stall_clock_restarts_when_the_pool_moves_again(scheduler) -> None:  # type: ignore[no-untyped-def]
    """涨了一点就重新给满耐心——一趟扫描中间有停顿是常态，不该被算成放弃。"""
    assert scheduler._scan_can_still_help(1, _pool(40), NOW) is True
    # 停一阵，但还没到判死。
    assert scheduler._scan_can_still_help(1, _pool(40), NOW + timedelta(seconds=100)) is True
    # 又涨了 → 停滞计时归零。
    assert scheduler._scan_can_still_help(1, _pool(55), NOW + timedelta(seconds=150)) is True
    # 从这一刻起再停同样久，仍然在耐心之内。
    later = NOW + timedelta(seconds=150) + SCAN_YIELD_PATIENCE - timedelta(seconds=1)

    assert scheduler._scan_can_still_help(1, _pool(55), later) is True


def test_each_task_keeps_its_own_books(scheduler) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **按任务记账**：一个星系的水位不许被另一个星系的读数顶掉。

    每个军力任务有自己的出发点、能打到的目标不一样，所以「涨没涨」是各自的事。
    共用一份记账的话，一个星系的数会被另一个星系的数当成自己的水位。

    ⚠️ **两个任务的数必须给成不一样的**，否则这条用例抓不住「共用一份记账」
    ——数相同时共用与不共用的结果完全一致（第一版就是这么写的，变异验证时
    「记账不按任务分开」照样全绿，才发现用例是空的）。

    这里的形状：任务 1 停在 40、任务 2 从 90 掉到 50。任务 2 自己的水位是 90，
    所以 50 是**没涨**、该起停滞的表；而共用一份记账的话读到的是任务 1 的 40，
    50 > 40 会被当成「涨了」、把表清零——于是耐心永远走不完。
    """
    assert scheduler._scan_can_still_help(1, _pool(40), NOW) is True
    assert scheduler._scan_can_still_help(2, _pool(90), NOW) is True

    # 任务 2 掉到 50：对它自己是没涨（水位 90），停滞的表从这一刻起走。
    tick = NOW + timedelta(seconds=5)
    assert scheduler._scan_can_still_help(2, _pool(50), tick) is True

    past = tick + SCAN_YIELD_PATIENCE + timedelta(seconds=1)

    # 共用记账的话，上一跳会被当成「涨了」，这里就还在耐心之内、答 True。
    assert scheduler._scan_can_still_help(2, _pool(50), past) is False
    # 而任务 1 的表压根还没起（它只被观察过一次），不受任务 2 的进度影响。
    assert scheduler._scan_can_still_help(1, _pool(40), past) is True


def test_a_pool_back_above_the_floor_clears_the_books(scheduler) -> None:  # type: ignore[no-untyped-def]
    """回到门限之上要把记账清干净，否则下一次缺货会**沿用上一次的停滞时刻**、
    一上来就判死。

    这一条钉的是 `pop(...)` 那两行。少了它，第二次缺货时耐心从「上一次开始停滞」
    算起，早就超了——于是让位只生效一次，往后再也不生效。
    """
    assert scheduler._scan_can_still_help(1, _pool(40), NOW) is True
    assert scheduler._scan_can_still_help(1, _pool(40), NOW + timedelta(seconds=10)) is True

    # 采够了，记账清空。
    assert scheduler._scan_can_still_help(1, _pool(120), NOW + timedelta(seconds=20)) is False

    # 一小时后又缺货：应该重新获得完整的耐心，而不是接着上一次的表。
    again = NOW + timedelta(hours=1)

    assert scheduler._scan_can_still_help(1, _pool(40), again) is True
    assert scheduler._scan_can_still_help(1, _pool(40), again + timedelta(seconds=30)) is True


def test_no_pool_at_all_never_yields(scheduler) -> None:  # type: ignore[no-untyped-def]
    """不是军力优先那条链路的任务没有这个池子，一律不让位、也不留记账。"""
    assert scheduler._scan_can_still_help(1, None, NOW) is False


# -- 这两条日志的限流 ----------------------------------------------------------
#
# 生产实测 2026-08-24 20:47–22:44：「不再让位」这条 WARNING 写了 2,084 行，
# `signature_changed` **全为 True**、`suppressed_since_last_log` 全是 0 或 1，
# 而按「态 + 窗口存货」去重之后只有 68 种内容。成因是签名当时取的是
# `_line_signature(message, payload)`，而正文与 payload 里都带着**每跳都在涨**的
# `stalled_seconds` —— 闸门于是每一跳都判成「状态变了」，限流整个失效。


def test_the_give_up_warning_is_written_once_per_window_not_once_per_tick(  # type: ignore[no-untyped-def]
    scheduler, clock, recorded: RecordingLog
) -> None:
    """⚠️⚠️ **只有「已经让了多久」在变时，不算状态变了。**

    「不再让位」是个**停在那里的**状态：判死之后 `_scan_can_still_help` 不再清任何
    账，于是每一跳都会再走一次这一支，而 `stalled` 一直在涨。把那个数算进签名，
    这条 WARNING 就变成每秒一条 —— 而它恰恰是「门限配得比榜上能采到的还高」
    唯一的可行动信号，刷屏等于把它埋掉。

    这里连打 60 跳，只有 `stalled` 在变：**只许写一条**。
    """
    scheduler._scan_can_still_help(1, _pool(89), NOW)  # noqa: SLF001
    # 第二次观察才起停滞的表（水位没涨）。
    stall_from = NOW + timedelta(seconds=1)
    scheduler._scan_can_still_help(1, _pool(89), stall_from)  # noqa: SLF001

    gave_up = stall_from + SCAN_YIELD_PATIENCE + timedelta(seconds=1)
    assert scheduler._scan_can_still_help(1, _pool(89), gave_up) is False  # noqa: SLF001
    assert len(recorded.gave_up) == 1
    level, message, payload = recorded.gave_up[0]
    assert level == "WARNING", "限流不许顺手把告警降级"
    assert "门限可能配得比榜上能采到的还高" in message, f"可行动的那句话没了：{message}"
    assert payload["stalled_seconds"] == 181, "排障要的那个数不许从 payload 里消失"

    _tick_until(
        scheduler,
        1,
        _pool(89),
        (gave_up + timedelta(seconds=second) for second in range(1, 60)),
    )

    assert len(recorded.gave_up) == 1, (
        f"只有 stalled 在涨却又写了 {len(recorded.gave_up)} 条：签名把时长算成状态了"
    )


def test_a_stuck_yield_still_gets_one_line_per_window(  # type: ignore[no-untyped-def]
    scheduler, clock, recorded: RecordingLog
) -> None:
    """⚠️ **压归压，不许压成沉默**：越过窗口那一条要照写，而且要如实交代压了几次。

    只断言「写了一条」是不够的 —— 把这条 WARNING 干脆不打也能让那条用例变绿，
    而那就把唯一的可行动信号删掉了。所以这里连**兜底那一条**和
    `suppressed_since_last_log` 的数一起钉。
    """
    stall_from = NOW + timedelta(seconds=1)
    scheduler._scan_can_still_help(1, _pool(89), NOW)  # noqa: SLF001
    scheduler._scan_can_still_help(1, _pool(89), stall_from)  # noqa: SLF001
    gave_up = stall_from + SCAN_YIELD_PATIENCE + timedelta(seconds=1)
    scheduler._scan_can_still_help(1, _pool(89), gave_up)  # noqa: SLF001

    # 59 跳被压掉，第 60 跳（窗口 +1 秒）落库。
    _tick_until(
        scheduler,
        1,
        _pool(89),
        (gave_up + timedelta(seconds=second) for second in range(1, 60)),
    )
    after = gave_up + REPEATED_LOG_WINDOW + timedelta(seconds=1)
    scheduler._scan_can_still_help(1, _pool(89), after)  # noqa: SLF001

    assert len(recorded.gave_up) == 2, "窗口过完了却一个字都不写：这叫压成沉默"
    _, message, payload = recorded.gave_up[1]
    assert payload["signature_changed"] is False, "状态没变，却被当成跃迁"
    assert payload["suppressed_since_last_log"] == 59
    assert payload["suppressed_span_seconds"] == round(REPEATED_LOG_WINDOW.total_seconds()) + 1
    assert "59 次" in message, f"被压掉几次没写进消息里：{message}"
    # 落库这一条上的 `stalled_seconds` 仍是它自己那一刻的实数。
    assert payload["stalled_seconds"] == round((after - stall_from).total_seconds())


def test_the_window_stock_changing_is_a_transition_and_writes_at_once(  # type: ignore[no-untyped-def]
    scheduler, clock, recorded: RecordingLog
) -> None:
    """⚠️ **「窗口内还剩几个」变了才叫状态变了** —— 那一条不受窗口压制，立刻写。

    这是限流最危险的失效方式的反面：签名要是缩得只剩一个布尔，存货从 89 掉到 85
    就会被压成沉默，而库里留着的上一条还在说 89。
    """
    stall_from = NOW + timedelta(seconds=1)
    scheduler._scan_can_still_help(1, _pool(89), NOW)  # noqa: SLF001
    scheduler._scan_can_still_help(1, _pool(89), stall_from)  # noqa: SLF001
    gave_up = stall_from + SCAN_YIELD_PATIENCE + timedelta(seconds=1)
    scheduler._scan_can_still_help(1, _pool(89), gave_up)  # noqa: SLF001
    assert len(recorded.gave_up) == 1

    # 窗口内掉了几个（读数过期）：窗口远没走完，但这一条要立刻写。
    moved = gave_up + timedelta(seconds=5)
    scheduler._scan_can_still_help(1, _pool(85), moved)  # noqa: SLF001

    assert len(recorded.gave_up) == 2, "存货变了却被压掉了：库里那条还在说 89"
    _, message, payload = recorded.gave_up[1]
    assert payload["in_window"] == 85
    assert payload["signature_changed"] is True
    assert "85 个" in message


def test_the_yield_notice_says_it_once_per_episode(  # type: ignore[no-untyped-def]
    scheduler, clock, recorded: RecordingLog
) -> None:
    """让位那条 INFO（`stalled=None` 那一支）不许有同样的毛病。

    它今天靠的是水位那道门（`seen is None` 才说话），不是靠限流；这条用例把那个
    结论钉住，免得往后有人挪动那道门时把 INFO 也放成每跳一条。

    形状：一整段让位期里池子一边涨一边停，跨过几十跳 —— 「这一跳让给军力榜」
    只该说一次。
    """
    scheduler._scan_can_still_help(1, _pool(40), NOW)  # noqa: SLF001
    assert len(recorded.yielding) == 1
    level, _, payload = recorded.yielding[0]
    assert level == "INFO", "让位本身是正常运转，不该是 WARNING"
    assert payload["stalled_seconds"] is None

    for second in range(1, 40):
        moment = NOW + timedelta(seconds=second)
        # 一半的跳数上池子在涨，另一半停着：两种都不该再说一遍。
        scheduler._scan_can_still_help(1, _pool(40 + second // 2), moment)  # noqa: SLF001

    assert len(recorded.yielding) == 1, "「让给军力榜去补货」变成了每跳一条"
    assert recorded.gave_up == [], "池子还在涨，不该有「不再让位」"


def test_a_real_military_task_actually_yields_through_the_facts(  # type: ignore[no-untyped-def]
    scheduler, repository, session_factory, launcher, clock, run_id
) -> None:
    """⚠️⚠️ **接线本身要有用例——判据和记账各自绿，不代表它们被接上了。**

    2026-08-24 生产实测踩到：`yields_to_a_scan` 与 `_scan_can_still_help` 的用例
    全绿，而生产上一条「让给军力榜去补货」都没有。原因是 `_facts` 里有**两处**
    构造 `TaskFacts`，军力任务走的是 `replace(base, ...)` 然后 `continue`，
    而那两格被加在了它走不到的另一支上 —— `military_window` 恒为 `None`，
    整条判据成了死代码。

    症状极具迷惑性：「扫描间隔让路」照常喊（它读的是**账号级**窗口，那个填得好好的），
    看上去一切正常，只是攻击从不让位。

    所以这条用例**必须从 `_facts` 走**，不能自己拼 `TaskFacts`：它钉的就是那根线。
    """
    bot = with_lines(repository, session_factory, (FIRST, 1), account_limit=9)
    # 池子里有目标、读数也新鲜，但**窗口门限设得比它多**，于是必然低于门限。
    for offset in range(3):
        target_near(session_factory, FIRST, offset=offset, score=9_000.0 - offset * 100)
    set_score_window(repository, max_age_hours=24, window_floor=99)
    scheduler.start()

    snapshot = scheduler.snapshot()
    facts = snapshot.facts
    snap = next(item for item in snapshot.snapshots if item.task_id == bot)

    pool = facts.of(snap).military_window
    assert pool is not None, "军力任务的 TaskFacts 上没有窗口存货——那两格没接上"
    assert pool.below_floor, f"这组夹具本该低于门限，实际 {pool.in_window}/{pool.floor}"
    assert facts.of(snap).scan_can_still_help, "第一次缺货就该判「补得进来」"
    assert not has_work(snap, facts), "窗口不够、补得进来 → 这一跳该让给军力榜"
