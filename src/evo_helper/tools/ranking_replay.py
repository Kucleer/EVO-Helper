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

先只支持「按现行算法重跑」。候选条件的对照留到 Phase 1a —— 那时新旧两套都从
同一份 `rows_raw` 出发，差异才归得到条件本身头上。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from evo_helper.config import Settings
from evo_helper.domain.models import Coordinate
from evo_helper.domain.ranking import RankingRow
from evo_helper.domain.records import RankingTarget
from evo_helper.game.ranking_ui import SCORE_ANCHOR_RESET_SCREENS
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.tools.ranking_scan import (
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


@dataclass
class ReplayResult:
    """重跑一遍的产物。"""

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


def replay(screens: Sequence[ScreenRecord], *, bot_limit: int | None = None) -> ReplayResult:
    """按**现行**算法把这几屏重跑一遍。

    ⚠️ 这里的每一步都得和 `scan()` 里那个循环对齐 —— 对齐没对齐由
    `verify(...)` 说，不由这段代码自己说。
    """
    result = ReplayResult()
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
            if fresh_valued > 0:
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
    args = parser.parse_args(argv)

    repository = SqlAlchemyRepository(
        create_session_factory(create_database_engine(Settings().database_url))
    )
    corpus = load(UUID(args.run_id), repository=repository)
    if not corpus.screens:
        print("这一趟没有逐屏语料（诊断开关没开，或者只开了一半）")
        return 1

    result = replay(corpus.screens, bot_limit=args.bot_limit)
    problems = verify(corpus, result)
    print(f"回放了 {len(result.screens)} 屏")
    print(f"  重置发生在：{result.resets or '没有'}")
    print(f"  收尾历史：{result.history_at_end} 点")
    print(f"  待补的洞：{result.holes} 个")
    if problems:
        print(f"\n⚠️ 回放和录下来的统计对不上（{len(problems)} 处）——结论一律不可信：")
        for line in problems[:20]:
            print(f"    {line}")
        return 2
    print("\n回放复现了录下来的全部统计。")
    print(json.dumps({"resets": result.resets, "history_at_end": result.history_at_end}))
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口
    raise SystemExit(main())
