"""自动停用之后「什么时候自己回来」这条判据本身。

判据是纯的，所以在这里钉得住；集成层钉的是「调度器有没有照它做」。分两层是有
理由的：**这一层是唯一能看见判据本身的地方。** 恢复那一下最后由
`repository.resume_mission_task` 在同一个事务里再确认一遍标记，于是集成层里几种
最要命的改错法（比如判据改成看 `enabled`）会被那道二次确认悄悄兜住——行没变、
日志没写、用例照样绿。只有直接调这几个函数才看得见。

⚠️⚠️ 全篇最要紧的是「用户自己关掉的任务永远不被碰」那几条。生产上
「侦查+攻击海盗」「扫描全星系 bot」「5 系攻击」「9 系攻击」四个是用户手动关的，
它们必须一直关着。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.domain.scheduler import (
    BACKOFF_SCHEME,
    BACKOFF_STEPS,
    DisabledRecovery,
    backoff_delay,
    due_for_a_backoff_retry,
)

NOW = datetime(2026, 8, 28, 1, 2, 49, tzinfo=UTC)
"""2026-08-28 那一夜六个任务被自动停用的那一刻，逐字取自生产日志。"""

REASON = "连续 3 次异常退出（退出码 1）"


# -- 退避曲线 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("round_number", "expected"),
    [
        (1, timedelta(minutes=15)),
        (2, timedelta(minutes=30)),
        (3, timedelta(hours=1)),
        (4, timedelta(hours=1)),
        (5, timedelta(hours=1)),
        (50, timedelta(hours=1)),
    ],
)
def test_the_backoff_curve_is_fifteen_thirty_then_capped_at_an_hour(
    round_number: int, expected: timedelta
) -> None:
    """用户口径（2026-08-28）：15 分 → 30 分 → 1 小时，之后一直 1 小时封顶。

    ⚠️ **第 4 轮起必须还是 1 小时。** 任何指数式的实现（翻倍、乘 1.5）在第 10 轮
    就是 128 小时——那和「再也不恢复」没有区别，正是这次要修的毛病。第 50 轮那一
    行专门守这个：它离封顶足够远，翻倍实现在这里必然溢出成一个荒唐的数。
    """
    assert backoff_delay(round_number) == expected


def test_the_curve_never_shrinks() -> None:
    """连着崩只能等得更久，不许更短。

    退避的全部作用是「防满速空转」；哪一档比前一档短，就等于在最该慢下来的时候
    加速——而那正是 `MAX_CONSECUTIVE_FAILURES` 当初要防的那个重启循环。
    """
    delays = [backoff_delay(index) for index in range(1, len(BACKOFF_STEPS) + 3)]

    assert delays == sorted(delays)


def test_a_nonsense_round_number_still_returns_a_sane_delay() -> None:
    """轮次算错了也不许抛异常——这个函数落在**收退出码**那条路上。

    为一个算错的数字把调度循环整个弄停，比多等 15 分钟糟得多。
    """
    assert backoff_delay(0) == BACKOFF_STEPS[0]
    assert backoff_delay(-7) == BACKOFF_STEPS[0]


def test_the_version_fingerprint_matches_the_curve() -> None:
    """payload 里那个版本指纹不许和真实曲线走散。

    它的用途是「事后从库里认出生产跑的是哪一版」。曲线改了而指纹没改，日志就会
    对着一条新曲线报旧版本号——那比不写指纹更糟，因为它会被当真。
    """
    minutes = [int(step.total_seconds() // 60) for step in BACKOFF_STEPS]

    assert BACKOFF_SCHEME.endswith(":15m-30m-60m")
    assert minutes == [15, 30, 60]


# -- ⚠️⚠️ 用户自己关掉的任务永远不被碰 -------------------------------------------


def test_a_task_the_user_switched_off_is_never_due() -> None:
    """**这一条是整次改动唯一不能出的错。**

    用户手动关掉走 `update_mission_task`，它把 `disabled_reason` 连同恢复标记、
    退避两列一起清成 NULL——所以库里的样子就是这一行：`enabled=False`，
    `disabled_reason IS NULL`。判据只认 `disabled_reason IS NOT NULL`。

    ⚠️ 把判据改成看 `enabled`（「关着的就放出来」），或者干脆去掉这一条，
    这一行当场转红。生产上有四个任务正处在这个状态。
    """
    assert (
        due_for_a_backoff_retry(
            disabled_reason=None,
            disabled_recovery=None,
            retry_after_utc=None,
            now=NOW,
        )
        is False
    )


def test_a_switched_off_task_with_a_stale_alarm_is_still_never_due() -> None:
    """更狠的一版：闹钟早就过期了，而且是**很久很久**以前。

    上面那条只摆了「两列都是 NULL」的现场，一个「看 `enabled`」的实现在那里可能
    因为别的原因也返回 False。这里把闹钟摆成 NULL 之外的样子仍然不许放行：
    停用原因为 NULL 就是「调度器没有关过它」，别的列写着什么都不作数。
    """
    assert (
        due_for_a_backoff_retry(
            disabled_reason=None,
            disabled_recovery=DisabledRecovery.BACKOFF.value,
            retry_after_utc=NOW - timedelta(days=30),
            now=NOW,
        )
        is False
    )


# -- 到点才放，别的标记一律不碰 ------------------------------------------------


def test_before_the_moment_it_is_not_due() -> None:
    """差一秒也不行。「快到了」不是「到了」。"""
    assert (
        due_for_a_backoff_retry(
            disabled_reason=REASON,
            disabled_recovery=DisabledRecovery.BACKOFF.value,
            retry_after_utc=NOW + timedelta(seconds=1),
            now=NOW,
        )
        is False
    )


def test_at_the_moment_it_is_due() -> None:
    """到点就放，边界含在内。

    含不含边界这一秒本身无关紧要，钉住它是为了让「到点了却不放」这一类改动
    （比如把 `<=` 写成 `<` 再配上一个只在整点 tick 的实现）有地方转红。
    """
    assert (
        due_for_a_backoff_retry(
            disabled_reason=REASON,
            disabled_recovery=DisabledRecovery.BACKOFF.value,
            retry_after_utc=NOW,
            now=NOW,
        )
        is True
    )


def test_a_line_shortage_disable_is_not_this_pass_business() -> None:
    """`FREE_LINES` 那一档走另一条路，这里一个字都不许碰它。

    两条恢复路径认的是不同的事实（「此刻有没有航线」vs「库里那个时刻到了没有」）。
    混起来的后果是航线不足的任务被按时间放出来——而那一刻航线可能仍然是满的，
    它会一放出来就再停一次，写出 2026-08-18 那种一小时 1368 行的日志。
    """
    assert (
        due_for_a_backoff_retry(
            disabled_reason="空闲航线不足，暂不启动 bot 攻击",
            disabled_recovery=DisabledRecovery.FREE_LINES.value,
            retry_after_utc=NOW - timedelta(hours=1),
            now=NOW,
        )
        is False
    )


def test_a_manual_disable_stays_manual() -> None:
    """参数填错那一类仍然要人工：改配置之前重试一万次都是同一个结果。"""
    assert (
        due_for_a_backoff_retry(
            disabled_reason="恒星系区间首尾颠倒",
            disabled_recovery=DisabledRecovery.MANUAL.value,
            retry_after_utc=NOW - timedelta(hours=1),
            now=NOW,
        )
        is False
    )


def test_a_backoff_disable_without_an_alarm_is_not_due() -> None:
    """标记是 `BACKOFF` 而闹钟为 NULL：认不出来就不动它。

    这个组合按构造出不来（写标记和写闹钟在同一个事务里），但判据必须自己顶住
    ——历史行、手改过的库、将来某次半截的迁移都可能把它摆出来，而「认不出来就
    放行」意味着一条不知道该等多久的链路每 tick 都被放出来一次。
    """
    assert (
        due_for_a_backoff_retry(
            disabled_reason=REASON,
            disabled_recovery=DisabledRecovery.BACKOFF.value,
            retry_after_utc=None,
            now=NOW,
        )
        is False
    )
