"""每一趟盲滚都要在 `system_log` 里留下一条，**落库不落文件**。

⚠️ **`rows_per_notch_observed` 是这条日志存在的全部理由。**
`ROWS_PER_NOTCH = 1.08` 只有 2 个样本、1 台机器、1 次会话（2026-08-22）。它一漂，
「盲滚 700 行」实际走的就不是 700 行——而这个偏差是**静默的**：不报错、不少一条
日志、页面上一切正常，只是采回来的 bot 静悄悄少一截。把每一轮的实测值记进库，
事后才答得出「这个标定还成不成立」，而不是等某天发现少采了再回头查。

⚠️ **落库而不是打文件。** 实机在另一台机器上，本地 `var/logs` 是陈旧的；
「标定还成不成立」这个问题要在控制台的日志页上答得出来。所以这份用例走的是
真的 sink → 真的 `system_log` 表 → 再查回来，而不是拦一个假的 `record_*`：
`payload_json` 是一列文本，序列化那一步坏了在假记录器上是看不见的。

⚠️ 全程不碰游戏：滚轮那一层是个假的，一个鼠标事件都不发。
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.game.ranking_nav import SpinResult
from evo_helper.game.ranking_ui import GLIDE_SETTLE_S, ROWS_PER_NOTCH, ROWS_PER_SCROLL
from evo_helper.infrastructure.system_log import (
    SystemLogContext,
    SystemLogSink,
    current_system_log_sink,
    install_system_log_sink,
    record_system_log,
    shutdown_system_log_sink,
)
from evo_helper.storage.system_log import SystemLogRepository
from evo_helper.tools.ranking_scan import (
    BlindSpinAccount,
    report_blind_spin,
    scroll_through_humans,
    spin_blind_rows,
)

HUMANS = "unkn0wn\nXXxxNAZIMxxXX\nhalo\nCocyte\n探险12"
BOTS = "bot_4_155_13\nbot_4_30_12\nbot_4_183_20"

#: 请求 700 行时按标定该发多少格。写成算式而不是 648：标定改了这份用例要跟着动，
#: 而不是留一个和标定脱钩的常数。
NOTCHES = round(700 / ROWS_PER_NOTCH)


@pytest.fixture
def logs(session_factory: sessionmaker[Session]) -> Iterator[SystemLogRepository]:
    """把 runner 的日志出口接到真的 `system_log` 表上。"""
    repository = SystemLogRepository(session_factory)
    install_system_log_sink(
        SystemLogSink(repository.append, flush_interval_s=0.01), context=SystemLogContext()
    )
    try:
        yield repository
    finally:
        shutdown_system_log_sink()


def _flush() -> None:
    sink = current_system_log_sink()
    assert sink is not None
    assert sink.flush(timeout=5)


def _run_one_pass(
    *,
    screens: list[str],
    blind_rows: int = 700,
    notches: int = NOTCHES,
    measured: int | None = 706,
    source: str = "default",
) -> None:
    """跑一趟假采集：盲滚一次连拨，检测段慢拖到见到 bot，然后把账落库。

    ⚠️ 这里刻意走 `scroll_through_humans` 而不是直接调 `report_blind_spin`：
    「这条日志是不是真的每趟都写」只有把它接在采集流程上才测得到，而那正是
    2026-08-18 排障时缺的那种证据。
    """
    at = 0
    account = BlindSpinAccount()

    def scroll() -> None:
        nonlocal at
        at = min(len(screens) - 1, at + 1)

    def record(message: str, payload: dict[str, object]) -> None:
        record_system_log("INFO", "tools.ranking_scan", message, payload=payload)

    stretch = scroll_through_humans(
        scroll=scroll,
        spin=lambda rows: spin_blind_rows(
            rows,
            spin=lambda requested: SpinResult(
                rows_requested=requested, notches=notches, spin_seconds=1.5
            ),
            measure_rows=lambda: measured,
            account=account,
        ),
        read_names=lambda: screens[at],
        wait=lambda _s: None,
        blind_rows=blind_rows,
        detection_budget=10,
        say_line=lambda _m: None,
        record=record,
        progress=None,
    )
    # 与 `scan()` 里同一道闸：一格都没拨就没有「这一趟盲滚」可记。
    if account.rows_requested:
        report_blind_spin(
            account,
            rows_to_bot_area=stretch.rows if stretch.reached_bots else None,
            source=source,
            record=record,
        )
    _flush()


def _blind_spin_payloads(logs: SystemLogRepository) -> list[dict[str, object]]:
    """库里那些盲滚记录的 `payload_json`，最新在前。

    判据是「带 `notches_sent` 的那些」而不是按正文匹配：正文是给人看的，
    早晚会被改一个字，而这一条记录的机器读法只有 `payload_json`。
    """
    page = logs.query(source="tools.ranking_scan", limit=50)
    return [
        payload
        for payload in (json.loads(entry.payload_json) for entry in page.rows)
        if "notches_sent" in payload
    ]


# -- ① 每趟一条，字段齐全 ------------------------------------------------------


def test_one_blind_spin_leaves_exactly_one_record(logs: SystemLogRepository) -> None:
    _run_one_pass(screens=[HUMANS, HUMANS, BOTS])

    assert len(_blind_spin_payloads(logs)) == 1


def test_the_payload_answers_whether_the_calibration_still_holds(
    logs: SystemLogRepository,
) -> None:
    """⚠️ **这一条是整份的要害。**

    要答得出「这个标定还成不成立」，库里就得同时有请求的行数、真发出去的格数、
    实测走到第几名，以及**当时代码里的标定值**。缺任何一个，事后就只能猜。
    """
    _run_one_pass(screens=[HUMANS, HUMANS, BOTS])

    payload = _blind_spin_payloads(logs)[0]

    assert payload["rows_requested"] == 700
    assert payload["notches_sent"] == NOTCHES
    assert payload["rows_measured"] == 706
    assert payload["rows_per_notch_observed"] == round(706 / NOTCHES, 3)
    assert payload["rows_per_notch_calibrated"] == ROWS_PER_NOTCH


def test_the_payload_carries_the_timing_and_the_provenance(logs: SystemLogRepository) -> None:
    """用时与来源都要留。

    用时是「每格 16ms 有没有被撑开」唯一的证据：Windows 上 `time.sleep` 的粒度是
    15.6ms，真被撑成 31ms/格的话动量就攒不起来，而症状同样是「拨了但没走」。
    """
    _run_one_pass(screens=[HUMANS, BOTS], source="cli")

    payload = _blind_spin_payloads(logs)[0]

    assert payload["spin_seconds"] == 1.5
    assert payload["glide_seconds"] == GLIDE_SETTLE_S
    assert payload["source"] == "cli"


def test_the_payload_carries_the_rows_it_took_to_reach_the_bot_area(
    logs: SystemLogRepository,
) -> None:
    """`rows_to_bot_area` 与「每格实测几行」必须在**同一条**里。

    分成两条就得靠时刻去凑，而一晚上跑三十多趟，凑错一次结论就反了。
    """
    _run_one_pass(screens=[HUMANS, HUMANS, HUMANS, BOTS])

    payload = _blind_spin_payloads(logs)[0]

    assert payload["rows_to_bot_area"] == 700 + round(3 * ROWS_PER_SCROLL)


# -- ② 反常的那几趟同样要留痕 --------------------------------------------------


def test_a_run_that_never_saw_a_bot_still_records_the_spin(logs: SystemLogRepository) -> None:
    """⚠️ **没到 bot 区那几趟是最需要这条记录的。**

    「盲滚走的距离不对」正是「翻满预算也没见到 bot」最可能的原因之一。这时候把
    记录省掉，剩下的就只有一句「仍没见到 bot」，而那句话答不出为什么。
    """
    _run_one_pass(screens=[HUMANS])

    payload = _blind_spin_payloads(logs)[0]

    assert payload["rows_to_bot_area"] is None
    assert payload["notches_sent"] == NOTCHES


def test_a_spin_whose_rows_could_not_be_measured_still_records_the_notches(
    logs: SystemLogRepository,
) -> None:
    """测不出每格几行也要留痕：格数与用时本身就说明「拨是拨出去了」。

    滚轮把列表停在非整行位置，逐行裁剪读出来的名次会横跨两行（实测过一屏只读出
    2 个名次），所以「测不出」是常态而不是故障。
    """
    _run_one_pass(screens=[HUMANS, BOTS], measured=None)

    payload = _blind_spin_payloads(logs)[0]

    assert payload["rows_measured"] is None
    assert payload["rows_per_notch_observed"] is None
    assert payload["notches_sent"] == NOTCHES


def test_a_drifted_calibration_is_visible_in_the_record(logs: SystemLogRepository) -> None:
    """标定漂了，库里这一条就得看得出来——不然这条日志白记。

    实发 648 格却只走了 400 行 = 每格 0.617 行，「盲滚 700 行」实际只走 400 行。
    这件事在别处一个字都看不出来。
    """
    _run_one_pass(screens=[HUMANS, BOTS], measured=400)

    observed = _blind_spin_payloads(logs)[0]["rows_per_notch_observed"]

    assert isinstance(observed, float)
    assert observed < ROWS_PER_NOTCH * 0.7


# -- ③ 一格都不拨的那一趟 ------------------------------------------------------


def test_zero_rows_never_writes_a_spin_record(logs: SystemLogRepository) -> None:
    """盲滚 0 行 = 一格都不拨，那就没有「这一趟盲滚」这回事可记。

    ⚠️ 这里断言的是**没有**那条记录，而不是「记了一条全 0 的」：一条全 0 的记录
    在日志页上和「拨了但一格都没走」长得一模一样，而后者是真故障。
    """
    _run_one_pass(screens=[HUMANS, BOTS], blind_rows=0, notches=0)

    assert _blind_spin_payloads(logs) == []
