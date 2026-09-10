"""回收作业**必须**在每条出口上落状态。

⚠️ **这条用例守的是一个真出过事的 bug。** 2026-09-10 23:14–23:39 实测：
`mark_recycle_job` 全仓没有调用点，作业永远停在 `pending`，于是调度器每一轮
都把同一批坐标当成待办再派一遍 —— `5:332:5` 连着四轮，而**攻击因为名额被
作业占满彻底停了**（近 25 分钟一发攻击都没有）。

所以这里断言的不是「状态好看」，是「**作业会被结掉**」这件事本身。
"""

from __future__ import annotations

from evo_helper.domain.records import Coordinate
from evo_helper.tools.bot_loop import BotLoop, BotOptions


class _Repo:
    """只记下「被要求结成什么」。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Coordinate, str]] = []

    def finish_recycle_job_for_target(
        self, *, target, origin, state, executed_at_utc, dispatch_id=None
    ):  # noqa: ANN001, ANN003, ARG002
        self.calls.append((target, state))
        return 42


def _loop_with(repo: _Repo) -> BotLoop:
    origin = Coordinate(5, 261, 8)
    loop = BotLoop(  # type: ignore[arg-type]
        object(), object(), BotOptions(targets=(), attack=False, origin=origin)
    )
    loop._repository = repo  # type: ignore[assignment]
    loop._run_id = None  # type: ignore[assignment]
    loop._ensure_run = lambda: (repo, None)  # type: ignore[assignment,method-assign]
    return loop


def test_no_debris_settles_the_job() -> None:
    """面板上没有「回收」按钮 ⇒ 作业结成 `no_debris`，不许留在 pending。"""
    repo = _Repo()
    loop = _loop_with(repo)
    target = Coordinate(5, 332, 5)

    loop._finish_recycle_job(target, "no_debris")

    assert repo.calls == [(target, "no_debris")]


def test_screen_error_is_not_no_debris() -> None:
    """⚠️ 画面异常**不许**报成「没残骸」。

    「去了、看清了、真没有」和「点错按钮、残骸框根本没弹出来」是两回事，
    善后完全不同 —— 同「实收为零 vs 解析失败」那条口径。
    """
    repo = _Repo()
    loop = _loop_with(repo)
    target = Coordinate(5, 332, 5)

    loop._finish_recycle_job(target, "screen_error")

    assert repo.calls == [(target, "screen_error")]
    assert repo.calls[0][1] != "no_debris"


def test_settling_never_raises_when_the_repository_is_gone() -> None:
    """取不到仓储时只记日志，**不抛** —— 记账不许把链路弄死。"""

    class _Broken:
        def finish_recycle_job_for_target(self, **_kwargs: object) -> None:
            raise RuntimeError("库连不上")

    repo = _Broken()
    loop = BotLoop(  # type: ignore[arg-type]
        object(), object(), BotOptions(targets=(), attack=False, origin=Coordinate(5, 261, 8))
    )
    loop._ensure_run = lambda: (repo, None)  # type: ignore[assignment,method-assign]

    loop._finish_recycle_job(Coordinate(1, 2, 3), "no_debris")  # 不抛就算过


def test_every_recycle_outcome_settles_the_job_source_level() -> None:
    """⚠️ **源码级断言：`_recycle_once` 的每条出口都要结作业。**

    上面那三条只证明「方法本身能用」，而真出事的 bug 是
    **方法存在、全仓没有一个调用点**（`mark_recycle_job` 就是这么躺了一整版）。
    这种接线错误**跑流程也看不出来** —— 那一轮看着「正常跳过」，
    只有几小时后才发现同一批坐标在无限重试。

    照 PR-A 那两条源码级断言的先例：直接读源码数出口。
    """
    import inspect

    from evo_helper.tools import bot_loop as module

    source = inspect.getsource(module.BotLoop._recycle_once)
    settles = source.count("self._finish_recycle_job(")
    returns_false = source.count("return False")

    # 每一条 `return False` 出口，外加成功那一条，都要落状态。
    # 唯一豁免：面板核对没过（`TargetCheck` 那条）—— 下一轮还该再试。
    assert settles >= returns_false, (
        f"`_recycle_once` 有 {returns_false} 条失败出口，却只有 {settles} 处结作业；"
        "漏掉的那条会让作业永远 pending、每轮重试，攻击的名额被占死"
    )
    assert 'self._finish_recycle_job(coordinate, "dispatched")' in source, (
        "派出成功之后没有结作业 —— 那一条会被下一轮当成待办再派一次"
    )
