"""开工要不要翻信箱：冷却判据本身。

这一整个文件守的是一次持续两天的生产故障的修法。原来的判据是一个布尔开关，
默认关；于是 2026-08-15 21:40 之后攻击照派、战报一份没读，直到 08-17 才发现。

判据现在是**频率**而不是开关，所以这里钉三件事：

1. 冷却边界两侧（恰好 N 分钟 / 差一分钟）各自走哪一支；
2. **从没对过账时必须翻**——这条是承重的，不是补丁；
3. 翻与不翻的日志措辞**必须是两句不同的话**，且不翻那一句要带上上次翻的时刻。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from evo_helper.domain.reconcile_cooldown import RECONCILE_COOLDOWN, decide_reconcile

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


class TestTheCooldownBoundary:
    def test_exactly_the_cooldown_sweeps(self) -> None:
        """正好到点算到期。

        取 `>` 的话每一轮都要多等一个时钟颗粒，而边界含糊比多翻一次贵得多。
        """
        decision = decide_reconcile(
            last_reconciled_at_utc=NOW - RECONCILE_COOLDOWN,
            now=NOW,
        )

        assert decision.sweep is True

    def test_one_minute_short_of_the_cooldown_skips(self) -> None:
        """差一分钟就不翻。这一支正是闸门 B 当初真正想要的东西：

        航线逐条释放，调度器会频繁续跑 runner（生产库实测 BOT 续跑间隔中位数
        5.3 分钟），每一趟都进信箱是纯浪费。
        """
        decision = decide_reconcile(
            last_reconciled_at_utc=NOW - RECONCILE_COOLDOWN + timedelta(minutes=1),
            now=NOW,
        )

        assert decision.sweep is False

    def test_well_past_the_cooldown_sweeps(self) -> None:
        decision = decide_reconcile(
            last_reconciled_at_utc=NOW - timedelta(hours=3),
            now=NOW,
        )

        assert decision.sweep is True

    def test_the_window_is_measured_not_guessed(self) -> None:
        """⚠️ **冷却窗口必须夹在两条边界之间**，理由整段在模块头那张表上。

        - 下界：续跑间隔的中位数（生产实测 BOT 5.3 / PIRATE 10.8 分钟）。低于它，
          多数续跑照样各翻一趟，冷却形同虚设。
        - 上界：`scheduler_config.report_grace_minutes` 默认 30 分钟——过了预计
          战报时间再等这么久还读不到就判缺失。窗口逼近它就会自己制造缺失，
          而窗口 = 无穷大正是这次故障的形状。

        钉住这两条，是因为「把窗口设大一点」看起来永远是无害的优化。
        """
        assert timedelta(minutes=10.8) < RECONCILE_COOLDOWN < timedelta(minutes=30)


class TestNeverReconciledBefore:
    def test_a_link_that_never_reconciled_must_sweep(self) -> None:
        """⚠️ **这一条是承重的。**

        新库、换库、清库之后一次都没翻过信箱时，冷却判据手里没有任何依据。
        这时唯一安全的默认是**翻**。反过来把「没有记录」当成「刚翻过」，
        就是这次故障的形状——一个不作为的默认值，安静地把整条链路关掉。
        """
        decision = decide_reconcile(last_reconciled_at_utc=None, now=NOW)

        assert decision.sweep is True
        assert decision.elapsed is None

    def test_a_never_reconciled_link_sweeps_no_matter_how_long_the_window_is(self) -> None:
        """窗口调到多大都不能把这一支变成「不翻」。"""
        decision = decide_reconcile(
            last_reconciled_at_utc=None, now=NOW, cooldown=timedelta(days=3650)
        )

        assert decision.sweep is True


class TestForcing:
    def test_force_ignores_the_cooldown(self) -> None:
        """`--reconcile` 的语义现在是「强制翻一次，忽略冷却」，供手工排障。"""
        decision = decide_reconcile(
            last_reconciled_at_utc=NOW - timedelta(seconds=5), now=NOW, forced=True
        )

        assert decision.sweep is True
        assert decision.forced is True

    def test_not_forcing_leaves_the_cooldown_in_charge(self) -> None:
        decision = decide_reconcile(last_reconciled_at_utc=NOW - timedelta(seconds=5), now=NOW)

        assert decision.sweep is False
        assert decision.forced is False


class TestAClockThatWentBackwards:
    def test_a_future_timestamp_skips_rather_than_sweeping(self) -> None:
        """库里那一行比现在还新 = 另一台机器刚翻过。跳过是对的。"""
        decision = decide_reconcile(last_reconciled_at_utc=NOW + timedelta(minutes=5), now=NOW)

        assert decision.sweep is False


class TestTheTwoWordings:
    """⚠️ **翻与不翻绝不能共用一句话。**

    混着说正是这次故障拖了两天没被发现的直接原因：日志把「我找过了，没有」
    和「我根本没去找」说成了同一句。
    """

    def test_skipping_says_it_is_not_sweeping_and_when_it_last_did(self) -> None:
        last = NOW - timedelta(minutes=3)
        note = decide_reconcile(last_reconciled_at_utc=last, now=NOW).note

        assert "本轮不翻信箱" in note
        assert f"{last:%Y-%m-%d %H:%M:%S} UTC" in note, "不翻时必须说清上次是什么时候翻的"
        assert "3.0" in note, "距上次多久也要说，否则没人判断得了这是不是异常"

    def test_sweeping_says_it_is_sweeping(self) -> None:
        note = decide_reconcile(last_reconciled_at_utc=NOW - timedelta(hours=2), now=NOW).note

        assert "这一轮翻信箱" in note
        assert "本轮不翻信箱" not in note

    def test_the_two_notes_are_never_the_same_sentence(self) -> None:
        swept = decide_reconcile(last_reconciled_at_utc=NOW - timedelta(hours=2), now=NOW).note
        skipped = decide_reconcile(last_reconciled_at_utc=NOW - timedelta(minutes=1), now=NOW).note

        assert swept != skipped

    def test_a_link_that_never_reconciled_says_so(self) -> None:
        note = decide_reconcile(last_reconciled_at_utc=None, now=NOW).note

        assert "从没对过账" in note
        assert "本轮不翻信箱" not in note

    def test_forcing_says_it_was_asked_for(self) -> None:
        note = decide_reconcile(
            last_reconciled_at_utc=NOW - timedelta(seconds=5), now=NOW, forced=True
        ).note

        assert "--reconcile" in note
        assert "本轮不翻信箱" not in note
