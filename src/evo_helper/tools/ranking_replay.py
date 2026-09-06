"""拿录下来的逐屏原始行，离线重跑一遍采集循环。

## 为什么需要它

要换自愈阀的触发条件，就得回答「换了之后这一趟会变成什么样」。而这个问题
**观测不出来**：换条件之后历史会不同、后续采信也会不同，轨迹在第一次被去掉的
误重置之后就分叉了。影子计数只能给出「三种条件在**现行**轨迹上的比较」。

真正的对照只有一条路：把同一份**原始输入**喂给新旧两套算法，各自从头重建历史。
原始输入由 `tools.ranking_scan` 在诊断开关打开时录进日志（`采集一屏` 的
`rows_raw`），这个模块把它读回来重跑。

## ⚠️⚠️ 它是采集循环的第二份实现，所以它的验收是「必须复现录下来的统计」

`scan()` 里那个循环绑着导航、驱动、仓储，没法直接拿来跑离线数据。所以这里照它的
形状重写了一遍 —— 而「同一件事两份实现，两边迟早分家」是这个仓库反复栽过的坑。

**唯一的对策是让分家立刻暴露**：`replay()` 交出的逐屏统计与重置位置，必须和录
语料那一趟日志里的一模一样。对不上就说明两份实现已经漂了，此时**回放结果一律
不可信**，先修实现再谈对照。用例 `test_ranking_replay.py` 钉的就是这一条。

## 用法

    python -m evo_helper.tools.ranking_replay <run_id>
    python -m evo_helper.tools.ranking_replay <run_id> --condition old

## 配对回放：为什么影子计数不够

`采集一屏` 里的 `success_old / success_verdict / success_history` 只回答
「三个条件在**现行**轨迹的这一屏上分别成不成立」。它们**答不了**
「换掉条件之后整趟会变成什么样」—— 因为第一次被去掉的误重置之后历史就不同了，
后续采信、后续参照、后续丢弃全都跟着分叉。

`--condition` 就是那把对照的刀：同一份 `rows_raw`，各自从头重建历史，
比的是**收尾产出**（历史点数、洞的个数），而不是逐屏的成不成立。

⚠️ **只有采集循环此刻在用的那个条件（`VALVE_CONDITION`）能拿去 `verify()`。**
换了条件之后轨迹本就该分叉，拿去对，对不上是**应该的**，不是分家。
`verify()` 自己会拦（见 `_CURRENT`）。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.domain.records import RankingTarget
from evo_helper.game.ranking_ui import SCORE_ANCHOR_RESET_SCREENS
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.tools.ranking_scan import (
    VALVE_CONDITION,
    ScreenOutcome,
    judge_rows,
    next_score_anchor,
    take_batch_targets,
)


@dataclass(frozen=True)
class ScreenRecord:
    """录下来的一屏。`rows` 是原始读数，`prior_states` 是落库前的库内状态。"""

    screen_seq: int
    rows: list[RankingRow]
    prior_states: dict[str, dict[str, Any]]
    #: 录语料那一趟自己算出来的统计。回放必须复现它。
    recorded: dict[str, Any]


@dataclass(frozen=True)
class Corpus:
    """一趟录下来的全部语料。

    ⚠⚠ **重置位置必须和屏一起存。** 只比逐屏计数时，阀的行为漂了
    `verify` 看不见 —— 而重置位置正是 Phase 1a 唯一关心的那个量。
    我第一版就是这么错的：把真循环的 `screen_seq > 0` 闸改成 `if True`
    （首屏也进阀），回放的用例全部照绿。
    """

    screens: list[ScreenRecord]
    #: 录语料那一趟的自愈阀实际在第几屏响过。
    resets: list[int]


#: 自愈阀可选的成功条件。见 `_SUCCESS`。
Condition = Literal["old", "verdict", "history"]

#: 「这一屏算成功」的三种口径。**它们是成功条件，不是触发条件** ——
#: 阀在**连续两屏都不成功**时才响。
#:
#: - `old`：现行口径，本屏有新靶带出了军力值（`fresh_valued > 0`）。
#:   ⚠️ `fresh` 在去重、bot 过滤、插值**之后**，所以「判据读对了但靶被去重挡了」
#:   会被它算成失败 —— 那就是误重置。
#: - `verdict`：判据采信了至少一个值（`verdict_trusted > 0`）。
#: - `history`：曲线历史进了至少一点（`history_appended > 0`）。
_SUCCESS: dict[Condition, Callable[[ScreenOutcome, int], bool]] = {
    "old": lambda outcome, fresh_valued: fresh_valued > 0,
    "verdict": lambda outcome, fresh_valued: outcome.verdict_trusted > 0,
    "history": lambda outcome, fresh_valued: outcome.history_appended > 0,
}


#: 条件名 -> 条件名，只为把命令行上的 `str` 收窄成 `Condition`。
_CONDITIONS: dict[str, Condition] = {name: name for name in _SUCCESS}

#: 采集循环**此刻**真在用的那个条件。⚠️ 这一行是两份实现之间唯一的绑带 ——
#: 它一失效，`verify()` 就会把口径差异报成实现分家。
_CURRENT: Condition = _CONDITIONS[VALVE_CONDITION]


@dataclass
class ReplayResult:
    """重跑一遍的产物。"""

    #: 这一遍用的是哪个成功条件。⚠️ `verify()` 只认采集循环此刻在用的那个。
    condition: Condition = _CURRENT
    #: 逐屏统计，键与 `采集一屏` 的 payload 同名，好直接比对。
    screens: list[dict[str, Any]] = field(default_factory=list)
    #: 每次重置发生在第几屏。
    resets: list[int] = field(default_factory=list)
    #: 收尾时曲线历史多少点。
    history_at_end: int = 0
    #: 读到名次却没读出军力值的坐标数（补数的分母）。
    holes: int = 0


def rows_from_payload(raw: Sequence[dict[str, Any]]) -> list[RankingRow]:
    """把 `rows_raw` 还原成 `RankingRow`。

    ⚠️ **空值原样还原。** 名次读不出、分数读不出、坐标反解不出这三种 `None` 都是
    判据要处理的情形 —— 还原时补个默认值就等于把语料改了。
    """
    rows: list[RankingRow] = []
    for item in raw:
        text = item.get("coordinate")
        rows.append(
            RankingRow(
                rank=item.get("rank"),
                name=item.get("name") or "",
                score=item.get("score"),
                coordinate=None if text is None else _coordinate(str(text)),
            )
        )
    return rows


def _coordinate(text: str) -> Coordinate | None:
    parts = text.split(":")
    if len(parts) != 3:
        return None
    try:
        galaxy, system, position = (int(part) for part in parts)
    except ValueError:
        return None
    return Coordinate(galaxy, system, position)


def replay(
    screens: Sequence[ScreenRecord],
    *,
    bot_limit: int | None = None,
    condition: Condition | None = None,
) -> ReplayResult:
    """把这几屏重跑一遍。`condition` 选自愈阀用哪个成功条件。

    ⚠️ 这里的每一步都得和 `scan()` 里那个循环对齐 —— 对齐没对齐由
    `verify(...)` 说，不由这段代码自己说。而 `verify()` 只对
    `condition="old"` 说得上话（录语料那一趟跑的就是它）。

    ⚠️⚠️ **换条件之后轨迹会分叉，所以不能只比逐屏计数。** 第一次被去掉的
    误重置之后历史就不同了，后续采信、参照、丢弃全跟着变。要比的是
    **收尾产出**：`history_at_end` 和 `holes`。
    """
    # ⚠️⚠️ **默认必须跟着真循环走。** 写死 `"old"` 的话，采集那头换了条件而这里
    # 没跟上，`verify()` 就会把「两份实现分家」报成一堆逐屏计数不一致 ——
    # 而那句结论会让人去改本来对着的代码。
    condition = _CURRENT if condition is None else condition
    result = ReplayResult(condition=condition)
    succeeded = _SUCCESS[condition]
    history: list[tuple[int, float]] = []
    anchor: float | None = None
    blind = 0
    seen: set[Coordinate] = set()
    unread: list[RankingTarget] = []
    observed = datetime.now(UTC)

    for record in screens:
        outcome = judge_rows(record.rows, observed_at=observed, anchor=anchor, history=history)
        picked = take_batch_targets(outcome.targets, seen=seen, limit=bot_limit)
        unread.extend(target for target in picked if target.military_score is None)
        anchor = next_score_anchor(record.rows, anchor=anchor)
        fresh_valued = sum(1 for target in picked if target.military_score is not None)

        # 自愈阀：和 `scan()` 一样只对 `screen_seq > 0` 生效。
        if record.screen_seq > 0:
            if succeeded(outcome, fresh_valued):
                blind = 0
            else:
                blind += 1
                if blind >= SCORE_ANCHOR_RESET_SCREENS:
                    anchor = None
                    history.clear()
                    blind = 0
                    result.resets.append(record.screen_seq)

        result.screens.append(
            {
                "scroll": record.screen_seq,
                "rows_read": len(record.rows),
                "verdict_trusted": outcome.verdict_trusted,
                "verdict_positive": outcome.verdict_positive,
                "history_appended": outcome.history_appended,
                "history_new_ranks": outcome.history_new_ranks,
                "bot_measured": outcome.bot_measured,
                "fresh_valued": fresh_valued,
            }
        )

    result.history_at_end = len(history)
    result.holes = sum(1 for target in unread if target.military_rank is not None)
    return result


#: 回放必须逐字复现的那几个键。
VERIFIED_KEYS = (
    "rows_read",
    "verdict_trusted",
    "verdict_positive",
    "history_appended",
    "history_new_ranks",
    "bot_measured",
    "fresh_valued",
)


def verify(corpus: Corpus, result: ReplayResult) -> list[str]:
    """回放有没有复现录下来的统计。交回**不一致的说明**，空列表 = 对上了。

    ⚠️⚠️ **这是这个模块唯一的信任基础。** 它是采集循环的第二份实现，对不上就说明
    两份已经分家，此时任何回放结论都不成立 —— 先修实现，别急着解释差异。
    """
    # ⚠️⚠️ **候选条件的轨迹拿来对是没有意义的。** 录语料那一趟跑的是现行条件；
    # 换了条件之后第一次被去掉的误重置以后历史就不同了，对不上是**应该的**。
    # 这里必须拦住 —— 否则一次配对回放的正常分叉会被读成「两份实现分家」，
    # 而那句结论会让人去改本来对着的代码。
    if result.condition != _CURRENT:
        return [
            f"这一遍跑的是 {result.condition!r}，不是采集循环在用的 {_CURRENT!r}，"
            "不能拿去核对录下来的统计（换了条件轨迹本就该分叉）"
        ]
    problems: list[str] = []
    screens = corpus.screens
    if len(result.screens) != len(screens):
        problems.append(f"屏数对不上：录了 {len(screens)} 屏，回放跑了 {len(result.screens)} 屏")
        return problems
    # ⚠⚠ **重置位置要比。** 只比计数的话，阀漂了这里一声不响。
    if corpus.resets != result.resets:
        problems.append(f"重置位置对不上：录的是 {corpus.resets}，回放算出 {result.resets}")
    for record, replayed in zip(screens, result.screens, strict=True):
        for key in VERIFIED_KEYS:
            if key not in record.recorded:
                continue
            if record.recorded[key] != replayed[key]:
                problems.append(
                    f"第 {record.screen_seq} 屏 {key}："
                    f"录的是 {record.recorded[key]}，回放算出 {replayed[key]}"
                )
    return problems


def load(run_id: UUID, *, repository: Any) -> Corpus:
    """从库里把某一趟录下来的逐屏语料读出来，按屏序排好。

    ⚠️ 缺 `rows_raw` 的屏一律跳过并在结尾报数 —— 那说明那一趟没开诊断开关，
    或者只开了一半，两种都不该被当成完整语料。
    """
    screens: list[ScreenRecord] = []
    resets: list[int] = []
    for message, payload in repository.ranking_capture_payloads(run_id):
        if message == "军力锚点重置" and "screen_seq" in payload:
            resets.append(int(payload["screen_seq"]))
            continue
        if message != "采集一屏" or "rows_raw" not in payload:
            continue
        screens.append(
            ScreenRecord(
                screen_seq=int(payload["scroll"]),
                rows=rows_from_payload(payload["rows_raw"]),
                prior_states=dict(payload.get("prior_states") or {}),
                recorded=payload,
            )
        )
    screens.sort(key=lambda record: record.screen_seq)
    return Corpus(screens=screens, resets=sorted(resets))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线重跑一趟军力榜采集")
    parser.add_argument("run_id", help="录了逐屏语料的那一趟的 run_id")
    parser.add_argument("--bot-limit", type=int, default=None)
    parser.add_argument(
        "--condition",
        choices=(*_SUCCESS, "all"),
        default="all",
        help="自愈阀用哪个成功条件；all = 三个都跑一遍并排比（默认）",
    )
    args = parser.parse_args(argv)

    repository = SqlAlchemyRepository(
        create_session_factory(create_database_engine(Settings().database_url))
    )
    corpus = load(UUID(args.run_id), repository=repository)
    if not corpus.screens:
        print("这一趟没有逐屏语料（诊断开关没开，或者只开了一半）")
        return 1

    # ⚠️ 采集循环在用的那个条件先跑，而且**先核对**。核不上就不往下走 ——
    # 那说明回放和采集循环已经分家，此时任何条件对照都是拿一份坏了的实现在比。
    baseline = replay(corpus.screens, bot_limit=args.bot_limit, condition=_CURRENT)
    problems = verify(corpus, baseline)
    print(f"回放了 {len(baseline.screens)} 屏（语料 run_id {args.run_id}）")
    if problems:
        print(f"\n⚠️ 回放和录下来的统计对不上（{len(problems)} 处）——结论一律不可信：")
        for line in problems[:20]:
            print(f"    {line}")
        return 2
    print("回放复现了录下来的全部统计，下面的对照才作数。\n")

    # `_SUCCESS` 的键就是允许的条件名 —— argparse 的 choices 从它来，
    # 于是这里查表既做了收窄也保证了两处不会分家。
    conditions: tuple[Condition, ...] = (
        ("old", "verdict", "history")
        if args.condition == "all"
        else (_CONDITIONS[str(args.condition)],)
    )
    results: dict[str, ReplayResult] = {}
    print(f"{'条件':<12}{'重置':>6}{'收尾历史':>10}{'待补的洞':>10}   重置位置")
    for name in conditions:
        outcome = (
            baseline
            if name == _CURRENT
            else replay(corpus.screens, bot_limit=args.bot_limit, condition=name)
        )
        results[name] = outcome
        print(
            f"{name:<12}{len(outcome.resets):>6}{outcome.history_at_end:>10}"
            f"{outcome.holes:>10}   {outcome.resets or '—'}"
        )
    print()
    print(
        json.dumps(
            {
                name: {
                    "resets": result.resets,
                    "history_at_end": result.history_at_end,
                    "holes": result.holes,
                }
                for name, result in results.items()
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
