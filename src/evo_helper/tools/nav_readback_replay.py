"""把导航栏回读的**生产读数**拿回来，离线给任意一条裁决规则打分。

## 为什么要有这个东西

`agreed_value` 那条裁决规则栽过两次，两次都是同一个形状：**规则的安全论证架在
一条「配方只会这样错」的性质上，而那条性质只在本机语料上验过，语料恰好没覆盖
会出错的字形。**

- 2026-08-18：老规则「第一套读出非空就采纳」，依据是九张实拍上「不会读错」。
  上线后 **28 次回读 28 次对不上**——九张里八张的恒星系框都是 `137`，
  而 `137` 是这套字体里最结实的一个数。
- 2026-08-25（这一次）：新规则最后一条要求「其余非空读数都能解释成漏字」，依据是
  43 张实拍上「每套配方错法只有丢位」。生产读数里 `15`→`6`、`391`→`3931`、
  `391`→`331` 全是替换或凭空多位，**那条性质在实机上不成立**，于是第 3 条从
  「挡臆造的闸」变成了「正确读数的否决权」。

两次的共同点是**改规则时手里没有真实读数**。而 2026-08-18 补进 `payload_json`
的那几个字段（三个框 × 每套配方的原始读数 + `expected` + `verdict`）已经攒了
几百条真实证据在库里躺着。这个工具把它们捞出来，让「换成这条规则会怎样」变成
一次可以当场数出来的测量，而不是一次推理。

## 它能回答什么、不能回答什么

**能**：在这些**已经失败**的格子上，某条规则救回几个、弄错几个。

⚠️ **不能**：证明新规则在**本来就成功**的格子上不退步。三个框全对时这条日志根本
不写，所以这份语料结构性地只有失败样本。那一半由
`tests/integration/vision/test_nav_bar_values_live.py` 的 43 张实拍守着——
**两边都要看，缺一边就是又一次「只在能看见的样本上验证」。**

## 真值从哪来

拿 `expected`（那一轮的出发星球坐标）当真值。它是**先验不是铁证**：回读对不上
也可能是导航栏真的停在别处。所以打分按置信度分档，判据见 `Cell.confident`；
低置信的那一档单独列，**不并进结论**。

2026-08-25 那批数据里 1290 格**全部**落在高置信档——也就是说这几百次告警没有
一次是「导航栏真的停在别处」，全是我们读错。这与用户实机核对的说法一致
（「这里是有坐标可以识别的」）。分档仍然留着：哪天真出现导航栏跑偏，
它是唯一能把两件事分开的东西。

用法：

    python -m evo_helper.tools.nav_readback_replay                 # 连库
    python -m evo_helper.tools.nav_readback_replay --dump out.json # 存一份离线用
    python -m evo_helper.tools.nav_readback_replay --from-file out.json
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evo_helper.game.system_navigator import NAV_VALUE_MIN_VOTES, _is_dropped_from, agreed_value

#: 三个值框在 payload 里的键，顺序与坐标的三段一一对应。
BOX_KEYS = ("galaxy", "system", "position")

#: 这条告警的原文。**按 message 精确匹配**：库里另有几条也提「导航栏」的日志
#: （底部导航条那条链的），混进来就等于拿另一块像素的读数给这条规则打分。
MESSAGE = "导航栏回读对不上出发星球"


@dataclass(frozen=True)
class Cell:
    """一个值框的一次读数：五套配方的原文，加上那一位的真值先验。"""

    log_id: int
    at: str
    box: str
    reads: tuple[str, ...]
    wanted: str

    @property
    def confident(self) -> bool:
        """屏上写的是不是真的 `wanted` —— 见模块头「真值从哪来」。

        两条任一成立就算数：

        1. **有配方一字不差地读出了 `wanted`。** 屏上写着别的数、而某套配方恰好
           把它读成完整的 `wanted`，这种巧合可以排除。
        2. 每个非空数字读数都能解释成 `wanted` 漏了字。

        ⚠️ **第 1 条不能省。** 第一版只写了第 2 条，于是 `15 ← ['6','1','15','15','15']`
        因为那个替换型的 `6` 被划进「低置信」——而它恰恰是这次要修的主力形态，
        123 个格子里绝大多数都长这样。**判据把要测的东西过滤掉了，报表还是绿的。**
        """
        if self.wanted in self.reads:
            return True
        return all(
            text == self.wanted or _is_dropped_from(text, self.wanted)
            for text in self.reads
            if text.isdigit()
        )


def cells(rows: Iterable[dict[str, Any]]) -> list[Cell]:
    """把 payload 摊平成一格一条。

    ⚠️ 三个框**全都要**，不只是判错的那个。一条规则的代价一半在「本来对的格子
    被它弄错了几个」，而那些格子就在同一条日志里躺着——只看失败格会把这一半
    结构性地看不见。
    """
    out: list[Cell] = []
    for row in rows:
        reads = row.get("reads") or {}
        parts = str(row.get("expected", "")).split(":")
        if len(parts) != len(BOX_KEYS):
            continue
        for key, wanted in zip(BOX_KEYS, parts, strict=True):
            if key not in reads:
                continue
            out.append(
                Cell(
                    log_id=int(row.get("id", 0)),
                    at=str(row.get("at", "")),
                    box=key,
                    reads=tuple(str(text) for text in reads[key]),
                    wanted=wanted,
                )
            )
    return out


# -- 参赛的几条规则 --------------------------------------------------------------


def rule_2026_08_18(reads: Sequence[str]) -> str:
    """2026-08-18 上线、2026-08-25 被这份数据判掉的那条规则的**冻结副本**。

    ⚠️ 这里**故意抄了一份**而不是 import：它存在的唯一意义是当基线，
    而基线必须在 `agreed_value` 改了之后仍然是同一条规则。跟着改的基线不是基线。
    """
    values = [text for text in reads if text.isdigit()]
    tally = Counter(values)
    candidates = [text for text, votes in tally.items() if votes >= NAV_VALUE_MIN_VOTES]
    if not candidates:
        return ""
    best = max(candidates, key=len)
    if any(text != best and not _is_dropped_from(text, best) for text in values):
        return ""
    return best


def rule_plurality(reads: Sequence[str]) -> str:
    """众数，平票取长。**没有否决权那一条。**"""
    values = [text for text in reads if text.isdigit()]
    if not values:
        return ""
    tally = Counter(values)
    top = max(tally.values())
    return max((text for text, votes in tally.items() if votes == top), key=len)


def rule_promote_longest(reads: Sequence[str]) -> str:
    """众数 + 「若有更长读数能解释掉全部其它读数，就提升到它」。

    ⚠️ **这条是墓碑，不是候选。** 它在 `261 ← ['261','26','26','6','61']` 上是对的
    （那几个短读数都只是 `261` 漏了位），却在
    `391 ← ['3','3931','391','331','391']` 上把本来能读对的 `391` 改成 `3931`。

    两组读数的**投票形态完全一样**：2 票的短值 + 1 票的长值 + 若干碎片，
    一个是漏字、一个是多字。**光看读数分不开这两者**——所以「正确值票数不够」
    那一类救不回来，要救必须引入一个独立于 OCR 的信号（数屏上有几位数字）。

    留在这里是为了让下一个想到这个主意的人当场看到它的代价。
    """
    values = [text for text in reads if text.isdigit()]
    if not values:
        return ""
    winner = rule_plurality(reads)
    for longer in sorted(set(values), key=len, reverse=True):
        if len(longer) <= len(winner):
            break
        if all(text == longer or _is_dropped_from(text, longer) for text in values):
            return longer
    return winner


def rule_live(reads: Sequence[str]) -> str:
    """仓里此刻真正在跑的那一条。"""
    return agreed_value(list(reads))


#: 参赛表。**基线必须排第一**——`report` 拿第一条当「较基线」的参照。
#:
#: 2026-08-25 这四条在 430 条生产告警（1290 个值框）上的成绩：
#:
#:     规则                读对   交空   读错
#:     2026-08-18(基线)     831    361     98
#:     众数                 991     67    232   ← 多读对 160，但多读错 134
#:     众数+提升(墓碑)      1088     67    135   ← 多读对 257，多读错 37
#:     在跑的（窄化否决）    954    238     98   ← 多读对 123，**一个都没多读错**
#:
#: ⚠️ 只看「读对」会挑中「提升」那一条。**「读错」和「交空」不是一回事**：
#: 交空只是下一个目标多重设一个字段，读错是拿一个可能不对的坐标去确认。
#: 这条链路的方向永远是「拿不准就多设」，所以选的是唯一没有多读错的那条。
RULES: dict[str, Callable[[Sequence[str]], str]] = {
    "2026-08-18(基线)": rule_2026_08_18,
    "众数": rule_plurality,
    "众数+提升(墓碑)": rule_promote_longest,
    "在跑的": rule_live,
}


# -- 打分 ------------------------------------------------------------------------


@dataclass
class Score:
    """一条规则的成绩。三档**不能合并**。

    `blank`（交空串）是「说不知道」，代价只是下一个目标多重设一个字段；
    `wrong`（交了个错的）是「说了假话」，代价是可能拿错坐标去确认。
    并成「不对」会让一条更爱猜的规则看起来和一条更谨慎的一样好。
    """

    right: int = 0
    blank: int = 0
    wrong: int = 0


def score(rule: Callable[[Sequence[str]], str], group: Sequence[Cell]) -> Score:
    out = Score()
    for cell in group:
        got = rule(cell.reads)
        if got == cell.wanted:
            out.right += 1
        elif not got:
            out.blank += 1
        else:
            out.wrong += 1
    return out


def report(group: Sequence[Cell], *, title: str) -> None:
    print(f"\n== {title}（{len(group)} 格）==")
    if not group:
        return
    baseline = [rule_2026_08_18(cell.reads) for cell in group]
    print(f"{'规则':<20}{'读对':>6}{'交空':>6}{'读错':>6}{'较基线救回':>12}{'较基线弄错':>12}")
    for name, rule in RULES.items():
        result = score(rule, group)
        got = [rule(cell.reads) for cell in group]
        rescued = sum(
            1
            for cell, was, now in zip(group, baseline, got, strict=True)
            if was != cell.wanted and now == cell.wanted
        )
        broken = sum(
            1
            for cell, was, now in zip(group, baseline, got, strict=True)
            if was == cell.wanted and now != cell.wanted
        )
        print(
            f"{name:<20}{result.right:>6}{result.blank:>6}{result.wrong:>6}"
            f"{rescued:>12}{broken:>12}"
        )


def worst_offenders(group: Sequence[Cell], rule: Callable[[Sequence[str]], str]) -> None:
    """这条规则仍然读不出的格子，按「真值 → 读数集合」归类。

    ⚠️ 这一段是**下一阶段的输入**，不是装饰：剩下的那些格子按真值聚起来，直接
    告诉我们该往实拍语料里补哪几个字形（现有 43 张里缺的正是它们）。
    """
    stuck = Counter(
        (cell.wanted, " ".join(text or "·" for text in cell.reads))
        for cell in group
        if rule(cell.reads) != cell.wanted
    )
    if not stuck:
        return
    print("\n-- 仍然读不出的格子（真值 ← 五套读数）--")
    for (wanted, reads), count in stuck.most_common(12):
        print(f"{count:>4}×  {wanted:>4} ← {reads}")


# -- 取数 ------------------------------------------------------------------------


def from_database() -> list[dict[str, Any]]:
    """从库里捞。**只读。**"""
    from sqlalchemy import text

    from evo_helper.config import Settings
    from evo_helper.storage.database import create_database_engine

    sql = text(
        "SELECT id, logged_at_utc, payload_json FROM system_log "
        "WHERE message = :message ORDER BY id"
    )
    rows: list[dict[str, Any]] = []
    with create_database_engine(Settings().database_url).connect() as conn:
        for log_id, at, raw in conn.execute(sql, {"message": MESSAGE}):
            try:
                payload = json.loads(raw or "{}")
            except ValueError:
                continue
            # 图不参与打分，而它是 payload 里最大的一块——留着会让离线文件大上百倍。
            payload.pop("thumbnail_png_base64", None)
            rows.append({"id": log_id, "at": at.isoformat(), **payload})
    return rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导航栏回读裁决规则的离线复盘台")
    parser.add_argument("--from-file", type=Path, help="读之前 --dump 存下的 JSON，不连库")
    parser.add_argument("--dump", type=Path, help="把捞到的读数存一份，供离线复跑")
    args = parser.parse_args(argv)

    rows = (
        json.loads(args.from_file.read_text(encoding="utf-8"))
        if args.from_file
        else from_database()
    )
    if args.dump:
        args.dump.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        print(f"存了 {len(rows)} 条到 {args.dump}")

    everything = cells(rows)
    confident = [cell for cell in everything if cell.confident]
    print(f"日志 {len(rows)} 条 → 值框 {len(everything)} 格，其中高置信 {len(confident)} 格")

    report(confident, title="高置信 · 合计")
    for key in BOX_KEYS:
        report([cell for cell in confident if cell.box == key], title=f"高置信 · {key}")
    report([cell for cell in everything if not cell.confident], title="低置信（不并进结论）")
    worst_offenders(confident, rule_plurality)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
