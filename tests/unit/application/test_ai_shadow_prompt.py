"""`build_prompt` 的口径。**这个文件以前根本不存在**（2026-08-19 审查发现：
全仓 grep 不到一处 `build_prompt` 的引用）。

后果是 prompt 里那几条最要紧的规矩全靠人读代码来保证：不许出现旋钮的值、
不许出现真实坐标、收益公式的系数、抽样怎么说。下一次改 prompt 的人可以**静默地**
把它们放回去而没有任何东西转红。这里把它们一条条钉住。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from evo_helper.application.ai_targeting import (
    EXAMPLE_ORIGIN,
    EXAMPLE_TARGET,
    NO_READING_BUCKET,
    OUTPUT_EXAMPLE,
    build_prompt,
)
from evo_helper.domain.ai_targeting import InflightLine
from evo_helper.domain.models import Coordinate
from evo_helper.domain.target_order import ScoredTarget

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
CYCLE_START = datetime(2026, 8, 17, 0, 0, tzinfo=UTC)

ORIGIN_A = Coordinate(4, 277, 15)
ORIGIN_B = Coordinate(9, 250, 8)

#: 坐标形状的记号：`银河:恒星系:行星位`。
COORDINATE_PATTERN = re.compile(r"\b\d{1,2}:\d{1,3}:\d{1,2}\b")


def _pool(count: int = 12, *, without_reading: int = 0) -> list[ScoredTarget]:
    items: list[ScoredTarget] = []
    for index in range(count):
        # ⚠️ 恒星系只有 1--499 环，越界会让 `flight_time` 当场炸
        # （`distance_units` 算出负数）。这不是用例的题目，别踩。
        coordinate = Coordinate(1 + index % 6, 1 + (index * 37) % 499, 1 + index % 9)
        if index < without_reading:
            items.append(ScoredTarget(coordinate=coordinate))
            continue
        items.append(
            ScoredTarget(
                coordinate=coordinate,
                military_score=float(8_000 + index * 2_500),
                military_score_at_utc=NOW - timedelta(hours=index * 0.7),
            )
        )
    return items


def _prompt(**overrides: object) -> str:
    pool = overrides.pop("candidates", None) or _pool()
    kwargs: dict[str, object] = {
        "task_id": 2,
        "now": NOW,
        "cycle_start": CYCLE_START,
        "origins": [ORIGIN_A, ORIGIN_B],
        "inflight_by_origin": {
            ORIGIN_A: [
                InflightLine(
                    dispatched_at_utc=NOW - timedelta(minutes=30),
                    line_free_at_utc=NOW + timedelta(minutes=20),
                ),
                InflightLine(dispatched_at_utc=NOW - timedelta(minutes=10), line_free_at_utc=None),
            ]
        },
        "configured_lines": {ORIGIN_A: 5, ORIGIN_B: 4},
        "budgets_by_origin": {ORIGIN_A: 3, ORIGIN_B: 2},
        "account_limit": 10,
        "account_inflight": 2,
        "candidates": pool,
        "presets": frozenset({"BBB", "CCC"}),
        "last_attack_at": {},
        "protected_seen_at": {},
        "sample_size": 30,
    }
    kwargs.update(overrides)
    return build_prompt(**kwargs)  # type: ignore[arg-type]


class TestNoRealCoordinatesInTheTemplate:
    """⚠️ **本仓库是公开的，prompt 模板会进 git**（需求 4.4）。

    上一版的输出示例里写着 `"origin": "4:277:15"`——那是用户真实的出发星球，
    而 changelog 同时宣称「模板不含真实坐标」。两件事都得修。
    """

    def test_the_output_example_only_uses_placeholder_coordinates(self) -> None:
        found = set(COORDINATE_PATTERN.findall(OUTPUT_EXAMPLE))
        assert found <= {EXAMPLE_TARGET, EXAMPLE_ORIGIN}, f"示例里混进了别的坐标：{found}"

    def test_the_placeholders_are_obviously_fictional(self) -> None:
        """0 号银河不存在——照抄示例会被硬校验当场作废，而不是混进记录里。"""
        for placeholder in (EXAMPLE_TARGET, EXAMPLE_ORIGIN):
            assert placeholder.startswith("0:"), f"{placeholder} 看起来像个真坐标"

    def test_the_users_real_origin_never_appears_as_an_example(self) -> None:
        """`4:277:15` 只该以「本轮真实出发星球」的身份出现，不该是模板里的例子。"""
        assert "4:277:15" not in OUTPUT_EXAMPLE


class TestTheKnobsAreNotLeaked:
    """⚠️ **五个旋钮的值一个都不许进 prompt**（需求 4.4，方案第一节）。

    AI 的定位就是替代它们；给了就等于把答案先塞给它。
    """

    def test_no_knob_names_appear(self) -> None:
        prompt = _prompt()
        for knob in (
            "score_max_age_hours",
            "top_n",
            "bot_revisit_hours",
            "max_score",
            "军力上限",
            "窗口门限",
            "重复攻击间隔",
        ):
            assert knob not in prompt, f"prompt 里出现了旋钮「{knob}」"

    def test_the_twenty_four_hour_revisit_window_is_never_mentioned(self) -> None:
        """只许提游戏规则的 8 小时保护期，不许提我们那个 24 小时策略。"""
        prompt = _prompt()
        assert "8 小时" in prompt
        assert "24 小时" not in prompt


class TestTheAgeDistributionIsReallyThere:
    """⚠️ 上一版写着「下面是…全池读数龄分布」而**后面什么都没有**。

    对模型说「有」而实际没有，比不说更糟：它会以为自己漏读了，或者干脆编一个。
    方案 2.3 要的是中位 / p90 / 最大 / 有多少个根本没读数。
    """

    def test_all_four_numbers_are_present(self) -> None:
        prompt = _prompt(candidates=_pool(20, without_reading=6))
        assert "全池读数龄分布" in prompt
        assert "中位" in prompt
        assert "p90" in prompt
        assert "最大" in prompt
        assert "6 个候选根本没有读数" in prompt

    def test_a_pool_with_no_readings_at_all_says_so_instead_of_faking_a_distribution(
        self,
    ) -> None:
        prompt = _prompt(candidates=_pool(5, without_reading=5))
        line = next(item for item in prompt.splitlines() if "全池读数龄分布" in item)
        assert "一个军力读数都没有" in line
        # ⚠️ 只挑那一行看：「p90」在飞行公式的适用域那一段里也出现，全文搜会误判。
        assert "p90" not in line


class TestTheSamplingDescriptionIsHonest:
    """⚠️ prompt 不许写死「每格取军力最高 3 + 读数最新 3」——抽样会按预算降配。"""

    def test_it_reports_what_was_actually_taken(self) -> None:
        prompt = _prompt(candidates=_pool(40), sample_size=6)
        assert "本次实取 6 个" in prompt
        assert re.search(r"覆盖 \d+/\d+ 个非空格子", prompt)
        assert re.search(r"单格最多 \d+ 个", prompt)

    def test_it_names_both_sampling_keys(self) -> None:
        """两个键都要点名——用「最强 / 最新」而不是现有得分正是为了不泄露答案。"""
        prompt = _prompt()
        assert "军力最高" in prompt
        assert "读数最新" in prompt

    def test_it_says_the_sample_is_not_the_whole_pool(self) -> None:
        prompt = _prompt()
        assert "分层抽样" in prompt
        assert "不是全池" in prompt

    def test_it_gives_the_real_pool_total(self) -> None:
        prompt = _prompt(candidates=_pool(40), sample_size=6)
        assert "全池共 40 个候选" in prompt


class TestTheFactsEachCandidateNeeds:
    """样本行必须带齐 AI 判断保护期与攻击间隔所需的原始事实（必修一第 2 条）。"""

    def test_a_never_attacked_target_says_so(self) -> None:
        prompt = _prompt()
        assert "我方从未打过" in prompt

    def test_a_recently_attacked_target_reports_how_long_ago(self) -> None:
        pool = _pool()
        prompt = _prompt(
            candidates=pool,
            last_attack_at={pool[0].coordinate: NOW - timedelta(hours=3)},
        )
        assert "我方上次攻击距今 3.0h" in prompt

    def test_a_target_that_hit_the_protection_period_is_marked(self) -> None:
        pool = _pool()
        prompt = _prompt(
            candidates=pool,
            protected_seen_at={pool[0].coordinate: NOW - timedelta(hours=2)},
        )
        assert "已知撞过保护期" in prompt

    def test_a_target_without_a_reading_says_so_in_its_row(self) -> None:
        """⚠️ 没读数 ≠ 军力接近 0。说成「很弱」AI 会整格跳过，而真相是我们不知道。"""
        prompt = _prompt(candidates=_pool(12, without_reading=12))
        assert "不是「很弱」" in prompt

    def test_the_cross_table_gives_no_reading_its_own_bucket(self) -> None:
        """★ 交叉表里「无读数」必须是**独立一档**，不许并进 `<10K`。

        ⚠️ 这一条和上面那条不是重复：样本行说的是**某一行**怎么写，这一条说的是
        **分桶**。并进 `<10K` 时样本行照样写着「无读数」，但摘要表会告诉 AI
        「这一格里全是 10K 以下的弱鸡」——那是句假话，AI 会整格跳过。
        （2026-08-19 变异实测：只有上面那条时，把分桶改回 `<10K` 一条都不红。）
        """
        prompt = _prompt(candidates=_pool(12, without_reading=12))
        # 交叉表那几行长这样：`    银河 4 | 往返 <30分 | 军力 无读数: 7 个，…`
        # ⚠️ 只认「银河」开头的那些——样本行里也有「| 军力 」，混进来会让断言失效。
        summary_rows = [
            line
            for line in prompt.splitlines()
            if line.strip().startswith("银河 ") and "军力" in line
        ]
        assert summary_rows, "交叉表整个不见了"
        buckets = {line.split("| 军力 ")[1].split(":")[0] for line in summary_rows}
        assert buckets == {NO_READING_BUCKET}, f"全是没读数的候选，交叉表却把它们分进了 {buckets}"
        assert "不是「很弱」" in prompt


class TestTheBudgetSection:
    def test_the_dispatchable_count_is_given_outright(self) -> None:
        """★ 用户特别要求：直接给可派发数，不让它自己做减法。"""
        prompt = _prompt()
        assert "本轮真实可派发数" in prompt
        assert "**5**" in prompt  # 3 + 2

    def test_the_unknown_duration_lines_are_flagged_separately(self) -> None:
        """⚠️ 「时长未知」那几条按兜底 90 分钟占着，不标出来 AI 会把猜测当事实。"""
        prompt = _prompt()
        assert "时长未知" in prompt
        assert "兜底 90 分钟" in prompt

    def test_the_account_wide_cap_is_given(self) -> None:
        assert "全账号航线上限 10" in _prompt()

    def test_no_account_cap_says_that_gate_is_off(self) -> None:
        assert "未配置全账号航线上限" in _prompt(account_limit=None)


class TestTheYieldModel:
    """收益公式的系数。⚠️ 线性模型必须配 −0.068，混用 −0.115 会算错（需求 4.3）。"""

    def test_the_linear_model_uses_the_right_decay(self) -> None:
        prompt = _prompt()
        assert "0.141 × 军力 × exp(−0.068 × 快照龄小时)" in prompt
        assert "−0.115" not in prompt

    def test_the_base_three_resources_are_ruled_out(self) -> None:
        """⚠️ 基础三样由我方货舱容量决定，拿它推「军力→收益」必得假结论。"""
        assert "基础三样（金属/晶体/气体）绝不能用来判断目标价值" in _prompt()

    def test_the_evidence_strength_is_labelled(self) -> None:
        """⚠️ 周内相位是零数据支撑的领域直觉，不标出来 AI 会当事实用。"""
        prompt = _prompt()
        assert "领域直觉、零数据支撑" in prompt


class TestNoSecretsOrIdentities:
    def test_the_player_name_never_appears(self) -> None:
        assert "Kucleer" not in _prompt()

    def test_no_api_key_shaped_string_appears(self) -> None:
        assert "sk-" not in _prompt()


def test_the_required_pick_count_matches_the_budget() -> None:
    prompt = _prompt()
    assert "picks **恰好** 5 个，不多不少" in prompt


def test_a_sample_size_below_one_is_rejected() -> None:
    with pytest.raises(ValueError):
        _prompt(sample_size=0)
