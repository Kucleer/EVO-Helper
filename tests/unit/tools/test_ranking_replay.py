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
    _CURRENT,
    _SUCCESS,
    Corpus,
    ScreenRecord,
    replay,
    rows_from_payload,
    verify,
)
from support.ranking_runs import (
    SCENARIO,
    SCENARIO_BLIND_START,
    SCENARIO_TRULY_BLIND,
    _trace,
)


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
    assert corpus.resets == [], (
        "`SCENARIO` 里那次重置是坐标重复引起的误重置，换了成功条件之后不该再发生"
    )

    # ⚠️ 但「语料记得下重置」这条性质还得有地方钉住 —— 换个真盲场景验。
    blind = _corpus(monkeypatch, SCENARIO_TRULY_BLIND)
    assert blind.resets, "真盲时阀该响，而语料里必须有这一条"


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
    """重置位置也要一样 —— 那是这一整件事唯一关心的那个量。

    ⚠️ 用真盲场景而不是 `SCENARIO`：后者那次是误重置，换了成功条件之后
    两边都不响了，**位置对不上也看不出来**（空列表等于空列表）。
    """
    corpus = _corpus(monkeypatch, SCENARIO_TRULY_BLIND)

    result = replay(corpus.screens)

    assert result.resets, "场景已经失效：真循环都没重置，那这条什么也证不了"
    assert result.resets == corpus.resets, (
        f"重置位置对不上：真循环 {corpus.resets}，回放 {result.resets}"
    )


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


def test_the_current_condition_skips_the_dedup_misfire(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️⚠️ **配对回放存在的理由：影子计数看不见级联。**

    `SCENARIO` 的第 3、4 屏坐标重复前两屏，于是去重后 `fresh` 为空 —— 现行条件
    据此认定「连续两屏什么都没采到」并清掉锚点与历史，**而判据其实把那几行都读对了**。

    换成候选条件之后重置不再发生，于是历史继续长：这一场景里 **5 点 → 17 点**。
    这个差额是**影子计数答不出来的** —— 它只回答「三个条件在现行轨迹的这一屏上
    分别成不成立」，而轨迹在第一次被去掉的误重置之后就分叉了。

    2026-09-06 实机第 1 趟（215 屏）上同一件事：现行条件重置 2 次、收尾 2060 点、
    洞 78 个；候选条件重置 **0** 次、2077 点、洞 72 个 —— 而影子计数预测的是
    「候选条件还会留下第 2 次重置」。**那第 2 次其实是第 1 次误重置造成的。**
    """
    corpus = _corpus(monkeypatch)

    before = replay(corpus.screens, condition="old")
    after = replay(corpus.screens)  # 默认 = 采集循环此刻在用的条件

    assert before.resets == [3], "旧条件在这一场景里必须误重置，否则场景已经失效"
    assert after.resets == [], "现行条件不该被「靶被去重挡了」当成一次失败"
    assert after.history_at_end > before.history_at_end, (
        f"去掉误重置之后历史该更长：旧条件 {before.history_at_end} 点，"
        f"现行 {after.history_at_end} 点"
    )


def test_a_genuinely_blind_stretch_still_resets_under_every_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **换条件的风险在反面：别把真盲也一起去掉了。**

    自愈阀唯一的存在理由就是「历史被毒了、判据在拿错参照丢整屏」时能自己脱身。
    候选条件如果连真盲都不认，阀就等于拆了 —— 而这件事在
    `SCENARIO`（误重置那一类）上一个字都看不出来。

    `SCENARIO_TRULY_BLIND` 里第 1、2 屏名次读得出、分数全 `None`，三个口径
    同时不成立。**三个条件都必须在同一屏响。**
    """
    corpus = _corpus(monkeypatch, SCENARIO_TRULY_BLIND)

    positions = {name: replay(corpus.screens, condition=name).resets for name in _SUCCESS}

    assert positions["old"], "场景已经失效：现行条件都没重置，那它测不到任何东西"
    assert len(set(map(tuple, positions.values()))) == 1, (
        f"真盲时三个条件必须一致，实际 {positions}"
    )


def test_verify_refuses_a_trajectory_from_another_condition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **换了条件的轨迹拿去核对录下来的统计，对不上是应该的。**

    录语料那一趟跑的是采集循环当时在用的条件。拿别的条件重跑一遍，轨迹本就分叉，
    此时 `verify` 报出的一堆「逐屏计数对不上」会被读成**两份实现分家** ——
    而那句结论会让人去改本来对着的代码。所以必须在 `verify` 里就拦住，
    不能靠调用方记得别这么用。
    """
    corpus = _corpus(monkeypatch)
    other = next(name for name in _SUCCESS if name != _CURRENT)

    problems = verify(corpus, replay(corpus.screens, condition=other))

    assert len(problems) == 1, f"该只报一条「条件不对」，实际 {problems}"
    assert other in problems[0] and _CURRENT in problems[0], (
        f"报出来的那句要把两个条件都说清楚：{problems[0]}"
    )
    assert verify(corpus, replay(corpus.screens)) == [], "采集循环在用的那个仍然要核对得上"
