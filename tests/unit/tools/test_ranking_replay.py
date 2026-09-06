"""回放工具**必须复现录下来的统计** —— 这是它唯一的信任基础。

`tools.ranking_replay` 是采集循环的**第二份实现**：`scan()` 里那个循环绑着导航、
驱动、仓储，没法直接拿来跑离线数据，所以照它的形状重写了一遍。

而「同一件事两份实现，两边迟早分家」是这个仓库反复栽过的坑
（`#275` 那次：日志另起一遍重算理由，而那一遍看到的历史比判据当时多，
于是它走到判据根本到不了的分支，交出一套看上去很像真的假话）。

**唯一的对策是让分家立刻暴露。** 所以这个文件做的事是：

1. 让真采集循环跑一趟、开着诊断开关，把逐屏原始行录下来
2. 把那份语料喂给回放工具
3. 断言回放算出的每一个计数都和录下来的**逐字相同**

对不上就说明两份实现已经漂了 —— 那时候任何回放结论都不成立，
先修实现，别急着解释差异。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.tools.ranking_replay import (
    Corpus,
    ScreenRecord,
    replay,
    rows_from_payload,
    verify,
)
from support.ranking_runs import SCENARIO, SCENARIO_BLIND_START, _trace


def _corpus(monkeypatch: pytest.MonkeyPatch, screens_in: object = SCENARIO) -> Corpus:
    """跑一趟真采集循环（开着开关），把逐屏语料收成回放的输入。"""
    run = _trace(monkeypatch, screens_in, capture_rows=True)  # type: ignore[arg-type]
    screens: list[ScreenRecord] = []
    for payload in run.payloads("采集一屏"):
        if "rows_raw" not in payload:
            continue
        screens.append(
            ScreenRecord(
                screen_seq=int(payload["scroll"]),
                rows=rows_from_payload(payload["rows_raw"]),
                prior_states=dict(payload.get("prior_states") or {}),
                recorded=payload,
            )
        )
    resets = [
        int(payload["screen_seq"])
        for payload in run.payloads("军力锚点重置")
        if "screen_seq" in payload
    ]
    return Corpus(screens=screens, resets=sorted(resets))


def test_the_corpus_covers_every_screen(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 前提：语料得是全的。屏序连续、不重复、不缺首屏。

    首屏原先根本不打「采集一屏」日志（它走在滚动循环外面），所以 `screen_seq=0`
    在不在，是这份语料能不能用的第一道门。
    """
    corpus = _corpus(monkeypatch)

    assert [record.screen_seq for record in corpus.screens] == [0, 1, 2, 3, 4, 5]
    assert all(record.rows for record in corpus.screens)
    assert corpus.resets == [3], "真阀在第 3 屏响过，语料里必须有这一条"


def test_the_replay_reproduces_every_recorded_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **本文件的重点。** 回放算出的每一个计数都要和录下来的逐字相同。

    这一条红了不代表回放工具坏了 —— 更可能是**采集循环变了而回放没跟上**
    （或者反过来）。两种都意味着「两份实现分家」，回放结论一律不可信。
    """
    corpus = _corpus(monkeypatch)

    problems = verify(corpus, replay(corpus.screens))

    assert problems == [], "回放和录下来的统计对不上：\n" + "\n".join(problems)


def test_the_replay_finds_the_same_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    """重置位置也要一样 —— 那是 Phase 1a 唯一关心的那个量。

    场景里第 3、4 屏坐标全部重复，于是去重后 `fresh` 为空、自愈阀在第 3 屏响。
    回放必须在同一个位置响。
    """
    corpus = _corpus(monkeypatch)

    result = replay(corpus.screens)

    assert result.resets == [3], f"重置位置对不上：{result.resets}"


def test_null_readings_survive_the_round_trip() -> None:
    """⚠️ **空值必须原样还原。**

    名次读不出、分数读不出、坐标反解不出这三种 `None` 正是判据要处理的情形。
    还原时补个默认值，回放就在一份被改过的输入上跑 —— 那比不回放更坏。
    """
    raw: list[dict[str, Any]] = [
        {"rank": None, "name": "bot_4_137_5", "score": 9_000.0, "coordinate": "4:137:5"},
        {"rank": 851, "name": "", "score": None, "coordinate": None},
    ]

    rows = rows_from_payload(raw)

    assert rows[0].rank is None
    assert rows[0].coordinate is not None
    assert rows[1].score is None
    assert rows[1].coordinate is None


def test_a_missing_screen_is_reported_not_swallowed() -> None:
    """屏数对不上时要说出来，而不是拿少一屏的结果去比。"""
    assert verify(Corpus(screens=[], resets=[]), replay([])) == []

    record = ScreenRecord(screen_seq=0, rows=[], prior_states={}, recorded={"rows_read": 0})
    assert verify(Corpus(screens=[record], resets=[]), replay([])) != []


def test_the_replay_catches_a_valve_that_drifted(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠⚠ **真循环的阀一旦和回放对不上，必须立刻报出来。**

    用首两屏都读不出值的场景 —— `SCENARIO` 里首屏是成功的，那里首屏
    进不进阀看不出差别（只是把失败计数归零）。这一份里两边的重置位置不同。

    ⚠️ 这条红了不代表回放工具坏了 —— 更可能是**采集循环变了而回放没跟上**。
    两种都意味着「两份实现分家」，回放结论一律不可信。
    """
    corpus = _corpus(monkeypatch, SCENARIO_BLIND_START)

    assert verify(corpus, replay(corpus.screens)) == []
