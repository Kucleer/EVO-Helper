"""这一轮开工到底要不要翻信箱：**按冷却算，不按开关算**。

## 为什么不是一个布尔开关

这个模块是从一次持续两天的生产故障里长出来的。攻击照常派（08-16 接受 71 发、
08-17 接受 15 发），而 `battle_reports` 从 2026-08-15 21:40 起一份都没有。
链路只有一条：`PirateLoop.run()` → `reconcile_today()` → `_scan_mail_rows()` →
`_ingest_report_row()` → `repository.append_report()`，而 `reconcile_today()`
全仓只有一个调用点。两道默认关闭的闸门叠在一起，**各自以「另一道还开着」为理由**：

- **闸门 A**（`web.schemas.SchedulerStartIn.reconcile`，默认 False）：注释里的
  兜底前提写的是「每一轮开工本来就有 `reconcile_today` 翻一趟信箱」。
- **闸门 B**（`LoopOptions.reconcile_on_start`，默认 False，加于两天后的
  2026-08-15 21:59——距最后一份战报 18 分钟）：自辩是「启动对账由控制台统一
  安排一次」，也就是推给了两天前刚以「B 还开着」为由关掉自己的 A。

而 B 一并加的 `--reconcile` 参数**没有任何生产者**：`domain.missions` 里
pirate 与 bot 两条命令行从来没有拼接过它，生产库 464 条 `mission_runs` 里
带 `reconcile` 的是 0 条。于是那个「统一安排的一次」一次都没发生过。

**但 B 的动机是对的。** 航线是逐条释放的，调度器会因此频繁续跑 runner；每个
runner 都进一趟信箱（实测约 83 秒，最多 8 页时要几分钟）就是纯浪费。它错在
**用布尔表达了一个本该是「频率」的意图**——「不要每次都翻」不等于「永远不翻」。

所以判据改成冷却：

- 距上次本链路对账 ≥ `RECONCILE_COOLDOWN` → 翻信箱
- 不足 → 跳过，并且**日志要说清跳过的理由和上次真正翻信箱的时刻**
- 从没对过账（`daily_reconciliations` 里一行都没有）→ 必须翻

第三条不是补丁而是承重：新库、换库、清库之后一次都没翻过信箱时，冷却判据
手里没有任何依据，这时**唯一安全的默认是翻**。反过来把「没有记录」当成
「刚翻过」，就是这次故障的形状——一个不作为的默认值，安静地把整条链路关掉。

`force` 那一档（命令行 `--reconcile`）留给手工排障：忽略冷却，强行翻一趟。
它**不接调度器**，调度器只走冷却。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: 两次翻信箱之间至少隔多久。
#:
#: ⚠️ **这个数是从生产库的实际起跑间隔量出来的，不是拍的。** 2026-08-17 只读查
#: 生产 `mission_runs` 最近 14 天、按 `(kind, task_id)` 分组的相邻起跑间隔
#: （分钟）：
#:
#: ==========  =====  =====  ======  ======  ======
#: 链路        n      min    p25     中位数  p75
#: ==========  =====  =====  ======  ======  ======
#: ``BOT``     209    5.0    5.0     5.3     10.8
#: ``PIRATE``  88     5.0    5.2     10.8    34.7
#: ==========  =====  =====  ======  ======  ======
#:
#: 5.0 那个地板是 `scheduler_config.restart_cooldown_seconds`（默认 300 秒）——
#: 续跑就是贴着它排队的，BOT 有 150/209 段间隔落在 5–10 分钟这一桶里。
#:
#: **下界**取续跑间隔的中位数：低于它，冷却就形同虚设（多数续跑照样各翻一趟，
#: 等于退回没有闸门 B 之前的浪费）。两条链路的中位数分别是 5.3 与 10.8，
#: 所以 N 必须 > 10.8。
#:
#: **上界**由「战报不能被拖到判缺失」定：`scheduler_config.report_grace_minutes`
#: 默认 30 分钟，过了预计战报时间再等这么久还读不到就判缺失跳过。冷却窗口逼近
#: 30 就会自己制造缺失——那正是这次故障的极端形态（窗口 = 无穷大）。
#:
#: 15 分钟落在 (10.8, 30) 的中间：对 BOT 那种 5 分钟一续的节奏，三趟续跑合成
#: 一趟信箱（省掉约 2/3 的翻箱开销，也就是闸门 B 真正想要的东西）；对 PIRATE
#: 那种 10.8 分钟的节奏也至少每两趟省一趟。代价是一份战报最多晚 15 分钟入库，
#: 距离 30 分钟的判缺失线还留着一半余量。
#:
#: 要改它先重跑那个分布：节奏（`restart_cooldown_seconds`、任务数）一变，
#: 上面两条边界跟着变，而这个数是**夹在两条边界之间**才成立的。
#:
#: ⚠️ **这是「没配置时」的默认值，不是唯一取值。** 它是一个**运维旋钮**，而且是
#: 旋钮里最该配的一种：它的两条边界本身就在库里可配（`restart_cooldown_seconds`
#: 定下界、`report_grace_minutes` 定上界），用户一改节奏，写死的 15 就不再夹在
#: 中间了。活动期间信箱堆积时也要调小。攻击配置页上有一个框
#: （`military_attack_config.reconcile_cooldown_minutes`），留空才走这里。
RECONCILE_COOLDOWN = timedelta(minutes=15)


@dataclass(frozen=True)
class ReconcileDecision:
    """这一轮翻不翻信箱，以及为什么。

    带上 `last_reconciled_at_utc` 是刻意的：它要一路传到日志措辞里去。
    「本轮没翻信箱」这句话单独说出来没有用——用户要判断的是「那这一发的战报
    到底有没有人去看过」，而回答那个问题的是**上次真正翻信箱的时刻**。
    """

    #: 这一轮要不要真的进信箱。
    sweep: bool
    #: 是不是被 `--reconcile` 强制的（忽略冷却）。
    forced: bool
    #: 上一次本链路真正翻完信箱、写下 `daily_reconciliations` 的时刻。
    #: None = 从来没对过账。
    last_reconciled_at_utc: datetime | None
    #: 判据用的冷却窗口，原样带出来供日志与测试引用。
    cooldown: timedelta
    #: 距上次对账过了多久。从没对过账时为 None。
    elapsed: timedelta | None

    @property
    def note(self) -> str:
        """写进日志的那一句。**翻与不翻用的是两套措辞，绝不共用一句。**

        混着说正是这次故障拖了两天没被发现的直接原因（见
        `tools.bot_loop.BotLoop._say_still_waiting`）。
        """
        if self.forced:
            return "开工对账：命令行显式要求（--reconcile），忽略冷却，这一轮翻信箱"
        if self.last_reconciled_at_utc is None:
            return "开工对账：本链路从没对过账（表里一行都没有），这一轮必须翻信箱"
        elapsed = _minutes(self.elapsed)
        window = _minutes(self.cooldown)
        stamp = f"{self.last_reconciled_at_utc:%Y-%m-%d %H:%M:%S} UTC"
        if self.sweep:
            return (
                f"开工对账：距上次对账（{stamp}）已 {elapsed} 分钟，"
                f"达到 {window} 分钟冷却，这一轮翻信箱"
            )
        return (
            f"开工对账：距上次对账（{stamp}）才 {elapsed} 分钟，"
            f"不足 {window} 分钟冷却，**本轮不翻信箱**"
        )


def decide_reconcile(
    *,
    last_reconciled_at_utc: datetime | None,
    now: datetime,
    forced: bool = False,
    cooldown: timedelta = RECONCILE_COOLDOWN,
) -> ReconcileDecision:
    """这一轮开工要不要翻信箱。

    边界取 **≥**：正好到点算到期。取 `>` 的话每次都要多等一个时钟颗粒，
    而这个判据的两侧都不是安全性问题，含糊比多翻一次贵。

    `last_reconciled_at_utc` 在未来（时钟回拨、库里那行是别的机器写的）时
    `elapsed` 会是负数，于是不到期、跳过——这是对的：另一台机器刚翻过。
    """
    elapsed = None if last_reconciled_at_utc is None else now - last_reconciled_at_utc
    if forced:
        sweep = True
    elif elapsed is None:
        # 从没对过账。手里没有任何依据时唯一安全的默认是**翻**，见模块头。
        sweep = True
    else:
        sweep = elapsed >= cooldown
    return ReconcileDecision(
        sweep=sweep,
        forced=forced,
        last_reconciled_at_utc=last_reconciled_at_utc,
        cooldown=cooldown,
        elapsed=elapsed,
    )


def _minutes(delta: timedelta | None) -> str:
    """把时长写成分钟。给人看的，所以取一位小数、不带单位。"""
    if delta is None:
        return "—"
    return f"{delta.total_seconds() / 60:.1f}"


__all__ = ["RECONCILE_COOLDOWN", "ReconcileDecision", "decide_reconcile"]
