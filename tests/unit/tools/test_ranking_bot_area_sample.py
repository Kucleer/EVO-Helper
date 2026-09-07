"""实测样本只有在**真读到 bot 行**的那一趟才留得下来。

## 为什么这条要单独一份用例

`翻了 N 行到达 bot 区` 那句话是盲滚自动标定**唯一**的样本来源
（`MissionScheduler._calibrated_blind_rows` 按前缀 `翻了 ` 从 `system_log` 里取，
`min(最近 5 次) - 83` 就是下一趟的盲滚行数）。

而 bot 区检测**会误报**：明明还在真人段，它宣布「到了」。样本原先就在那一刻发出去，
于是一趟采到 0 条的跑法照样留下一条偏小的实测：

    滚得不够 → 误判「到了」→ 记一个偏小的数 → 下一趟滚得更少 → 又误判 → 数更小

**它自我强化，而且永远转不回来。** 2026-09-07 早上真发生了：盲滚被压到 596 行
（bot 区在 821 行），7 趟里 6 趟一条都没采到，一直到有人手工把盲滚行数填死才停。

⚠️ **修法刻意不认机器名。** 用户口径（2026-09-07，逐字）：
「谁拉代码谁就是实体机，未来也可能有新的」。按 host 筛只是把问题挪走 ——
备份机上的调试跑一样会误报，而实机自己误报时照样没人挡。判据只有一条：
**这一趟到底有没有见到 bot。**
"""

from __future__ import annotations

import pytest

from evo_helper.domain.ranking import BOT_AREA_REACHED_PREFIX
from support.ranking_runs import SCENARIO, SCENARIO_NO_BOTS, _trace


def _samples(said: list[str]) -> list[str]:
    """`said` 里会被标定当成样本的那几行 —— 判据和标定那边逐字相同（前缀匹配）。"""
    return [line for line in said if line.startswith(BOT_AREA_REACHED_PREFIX)]


def test_a_run_that_reads_bots_leaves_exactly_one_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常一趟：样本照留，而且**只留一条**。

    留两条的后果比不留更隐蔽：`min(最近 5 次)` 的窗口会被同一趟灌满，于是标定
    从「看最近 5 趟」变成「看最近 2 趟」，而日志里样本条数照涨。
    """
    run = _trace(monkeypatch, SCENARIO)

    assert len(_samples(run.said)) == 1, f"样本行：{_samples(run.said)}"


def test_a_run_that_never_reads_a_bot_leaves_no_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠ **本文件的重点。** 检测段说「到了」，可整趟一个 bot 都没读到 —— 不许留样本。

    这一条红了就意味着 2026-09-07 那个自我强化的圈又通了：一趟空跑会把下一趟的
    盲滚行数压得更小，而下一趟更可能空跑。
    """
    run = _trace(monkeypatch, SCENARIO_NO_BOTS)

    assert _samples(run.said) == [], "一个 bot 都没读到，却留下了实测样本"


def test_the_empty_run_still_says_what_happened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ 不留样本 ≠ 不说话。**空跑必须仍然看得见**。

    否则这次修完的症状是「日志里一句都没有，而盲滚行数莫名其妙地不动了」——
    比原来的错更难查。检测段那句进度照打，只是刻意避开了 `翻了 ` 那个前缀。
    """
    run = _trace(monkeypatch, SCENARIO_NO_BOTS)

    progress = [line for line in run.said if "检测段判定已进 bot 区" in line]
    assert len(progress) == 1, f"检测段那句进度不见了：{run.said}"
    assert not progress[0].startswith(BOT_AREA_REACHED_PREFIX), (
        "进度句撞上了标定的取样前缀 —— 推迟就白做了"
    )


def test_the_sample_lands_after_the_first_screen_with_bots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """样本发在**第一次读到 bot 行之后**，不是检测段刚结束那一刻。

    顺序本身就是这次改动的内容：检测段的判断在前、样本在后。倒过来就等于
    没改（那时还不知道这一趟会不会读到 bot）。
    """
    run = _trace(monkeypatch, SCENARIO)

    progress = next(i for i, line in enumerate(run.said) if "检测段判定已进 bot 区" in line)
    sample = next(i for i, line in enumerate(run.said) if line.startswith(BOT_AREA_REACHED_PREFIX))

    assert progress < sample, "样本发得比检测段还早，等于没推迟"


def test_the_margin_warning_goes_with_the_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **余量告警和样本同进同退。**

    告警算的是同一个实测量（`rows - blind_rows`），数不作数、据它算出来的告警也
    不作数。2026-09-07 08:07 那条就误导了一次：它让人去查「攻击配置页上的盲滚
    行数是不是填得太大」，而那个数正是标定自己算错的。
    """
    run = _trace(monkeypatch, SCENARIO_NO_BOTS)

    assert not [line for line in run.said if "盲滚余量告急" in line], (
        "一个 bot 都没读到，却按那个假实测报了余量告急"
    )
