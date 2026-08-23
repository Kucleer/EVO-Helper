"""AI 选靶一期的观测侧：prompt 组装、LLM 调用、校验落库，全部 fire-and-forget。

⚠️ **这是全仓第一个对外网络调用。** 控制台进程从此有了一个外部依赖。
它挂了、慢了、限流了，**都不许影响调度**——本模块的所有出口都是「记一条日志、
照常派遣」，没有任何路径能让一次 LLM 失败停掉一轮攻击（需求文档第八节）。

## 一期的架构选择：发出去就不管

`_military_assignments`（同步调度 tick 里）调用 `observe()` 之后立刻返回——
真正组 prompt、调 LLM、校验、落库都在一个后台线程里做。一期不用 AI 的答案，
决策早已做完，这个调用是**纯观测**，不占用关键路径最自然。

⚠️ **这条路走不到二期。** 二期要按 AI 的结果派遣，它就必须回到关键路径上，
派遣得等它。那是一次真实的架构分叉，届时的失败模式（超时了到底派不派）
需要单独设计——现在就写下来，免得二期以为「一期已经打通了」（需求 2.2）。

## 线程纪律

- **同时最多一个后台线程在跑**，每任务有最小间隔（`AI_SHADOW_MIN_INTERVAL_S`）：
  不许每 tick 堆一个，也不许两个任务同时撞 API。
- **开关关掉时零开销**：`observe()` 第一行就查开关，不建线程、不组 prompt、
  不查库。
- **任何异常都不许让线程死而无痕**：worker 的外层 `finally` 兜底记一条
  `system_log`。
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from evo_helper.config import Settings
from evo_helper.domain.ai_targeting import (
    AiDecisionStatus,
    AiPick,
    AiTargetDecision,
    InflightLine,
    PickVocabulary,
    SoftReference,
    parse_pick,
    soft_check_picks,
    validate_picks,
)
from evo_helper.domain.bot_round import BOT_ATTACK_PRESET
from evo_helper.domain.flight_time import round_trip_hours
from evo_helper.domain.models import Coordinate
from evo_helper.domain.rules import cycle_start_utc
from evo_helper.domain.scan_bounds import SYSTEMS_PER_GALAXY, TOTAL_GALAXIES
from evo_helper.domain.target_order import GAME_PROTECTION_HOURS, ScoredTarget
from evo_helper.infrastructure.system_log import record_system_log
from evo_helper.storage.repository import SqlAlchemyRepository

try:  # pragma: no cover - 可选依赖：缺 httpx 时影子观测整个失效，调度不受影响。
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

#: 同时最多几个后台线程。1 就够了：一轮只该有一个影子观测在飞，
#: 多了只是排队撞同一个 API 限流。
MAX_CONCURRENT_WORKERS = 1

#: 同一个任务两次发起之间的最小间隔。调度器一轮攻击跑完几秒就再起一轮，
#: 不节流就是每秒一发 API 请求。60 秒：一轮约 4 发、跑几分钟，够密了。
AI_SHADOW_MIN_INTERVAL_S = 60.0

#: 默认样本量。**需求文档第七节写的是 60，这里是 120——刻意调大的。**
#:
#: ⚠️ 60 那个数是按「`eligible` 量级」估的。喂进去的池子换成 `candidates` 全池
#: 之后，交叉表的格子从约 40 涨到约 80（合成 3,788 个候选、两个出发点实测：
#: 80 个非空格子），而 60 连「每格一个代表」都不够——旧的降配阶梯会一路退到
#: 「每格只取军力最高 1 个」，**「读数最新」这个抽样键整个消失**。两个键刻意
#: 用「最强 / 最新」正是为了不把现有得分公式的答案泄露给 AI，丢掉一个就等于
#: 丢掉这个设计（方案 2.2）。
#:
#: 分类：**运维旋钮**（`military_attack_config.ai_sample_size`）。调大它看得更全、
#: prompt 更贵；调小会先减少每格的第二、第三个代表，格子覆盖率也跟着掉——
#: 掉到什么程度 prompt 会照实说（见 `_pool_sections`）。
DEFAULT_AI_SAMPLE_SIZE = 120

#: 每个格子每个抽样键最多取几个。**两个键各自的上限，不是合计。**
PER_CELL_PER_KEY = 3

#: 跳过路径的日志限流窗口（秒）。抄 `record_unrecognised_screen` 与
#: `mission_scheduler.REPEATED_LOG_WINDOW` 的 120 秒先例——同一类问题：
#: 一个每一轮攻击都可能触发的东西，不限流就把 `system_log` 淹掉。
#:
#: ⚠️ 「可用 ↔ 不可用」那一档**不走这个窗口**：它是状态跃迁，变了就立刻写
#: （同 `_log_a_repeated_line` 的两条规则）。
SKIP_LOG_THROTTLE_S = 120.0

#: 输出示例里用的坐标。**故意是不存在的 0 号银河**——本仓库是公开的，
#: prompt 模板会进 git，**模板里不许出现任何真实坐标当例子**（需求 4.4）。
#:
#: 它顺带是一道免费的检验：模型要是照抄示例，硬校验的 `unknown_target` /
#: `unknown_origin` 会当场把整份作废，而不是让一个假坐标混进记录里。
EXAMPLE_TARGET = "0:0:1"
EXAMPLE_ORIGIN = "0:0:2"

#: 默认单次 LLM 往返超时。需求文档第七节：默认 30 秒。
DEFAULT_AI_TIMEOUT_S = 30.0

#: 默认保留天数。需求文档第六节：默认 90 天。
DEFAULT_AI_RETENTION_DAYS = 90

#: 采样温度。0.2：让它输出稳定、少编数字；一期不需要创造性。
AI_TEMPERATURE = 0.2

#: 模型回复的 token 上限。picks 一般几十个目标，2000 足够，还省费用。
AI_MAX_TOKENS = 2000


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _trip_bucket(minutes: float) -> str:
    """往返分钟分档（prompt 里的交叉表用）。跨银河那一档基本都 ≥120。"""
    if minutes < 30:
        return "<30分"
    if minutes < 60:
        return "30–60分"
    if minutes < 90:
        return "60–90分"
    if minutes < 120:
        return "90–120分"
    return "≥120分"


#: 「军力榜从来没见过它」那一档的档名。
#:
#: ⚠️ **不许把没读数的并进 `<10K`。** 喂给 AI 的是 `candidates` 全池，里面本来
#: 就有一批从没上过军力榜的坐标；把它们算成「军力接近 0」是句假话，AI 会据此
#: 断定那一格全是弱鸡而整格跳过——而真相是**我们不知道**。
NO_READING_BUCKET = "无读数"


def _score_bucket(score: float | None) -> str:
    """军力分档（prompt 里的交叉表用）。**只是描述性分桶，不是选靶旋钮。**"""
    if score is None:
        return NO_READING_BUCKET
    value = score
    if value < 10_000:
        return "<10K"
    if value < 20_000:
        return "10–20K"
    if value < 40_000:
        return "20–40K"
    return "≥40K"


def _cell_key(origin: Coordinate, target: ScoredTarget) -> tuple[Coordinate, int, str, str]:
    minutes = round_trip_hours(target.coordinate, origin) * 60
    return (
        origin,
        target.coordinate.galaxy,
        _trip_bucket(minutes),
        _score_bucket(target.military_score),
    )


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _median_hours(moments: Sequence[datetime], now: datetime) -> float:
    hours = sorted((now - moment).total_seconds() / 3600 for moment in moments)
    mid = len(hours) // 2
    if len(hours) % 2:
        return hours[mid]
    return (hours[mid - 1] + hours[mid]) / 2


#: 排行「最新」用的哨兵：读数时刻为 None 的排最后（本来就不会到这里——
#: 第 2 步 `with_a_military_reading` 已经剔掉了，留着是给纯函数自己兜底）。
_MIN_AGE_UTC = datetime(1970, 1, 1, tzinfo=UTC)


def _strongest_first(target: ScoredTarget) -> tuple[bool, float, Coordinate]:
    """「军力最高」这个抽样键。没读数的排最后（不知道 ≠ 等于 0）。"""
    return (target.military_score is None, -(target.military_score or 0.0), target.coordinate)


def _freshest_first(target: ScoredTarget) -> tuple[bool, float, Coordinate]:
    """「读数最新」这个抽样键。没读数的排最后。"""
    return (
        target.military_score_at_utc is None,
        -_epoch(target.military_score_at_utc),
        target.coordinate,
    )


def _epoch(moment: datetime | None) -> float:
    """排序用的时刻数值。`_freshest_first` 要取负号，所以不能直接排 datetime。"""
    return _MIN_AGE_UTC.timestamp() if moment is None else moment.timestamp()


@dataclass(frozen=True)
class StratifiedSample:
    """分层抽样的结果，**外加它到底是怎么取的**。

    ⚠️ 后面那几个数不是装饰：prompt 里必须照实说每格实际取了几个、覆盖了几个
    格子。写死一句「每格取军力最高 3 + 读数最新 3」而实际降配成了别的，
    就是对模型说假话——而这个仓库的规矩是「日志说假话比不说更糟」，
    对 prompt 一样成立。
    """

    targets: tuple[ScoredTarget, ...]
    #: 交叉表里非空格子的总数。
    cells_total: int
    #: 真的分到了至少一个代表的格子数。
    cells_covered: int
    #: 单个格子里实际取到的最多几个。
    max_per_cell: int


def stratified_samples(
    candidates: Sequence[ScoredTarget],
    origins: Sequence[Coordinate],
    *,
    sample_size: int,
) -> StratifiedSample:
    """从**全池**里取分层样本：每个 (出发点, 银河, 往返档, 军力档) 格子里
    按「军力最高」与「读数最新」两个键各取至多 `PER_CELL_PER_KEY` 个。

    ⚠️ **抽样键刻意是「最强 / 最新」两个，不是现有得分。** 按 `军力 ÷ 往返`
    排序后取前 N 等于把要验证的那条公式的答案泄露给它（需求 4.2）。

    ## 预算紧的时候先砍什么

    旧版按「3+3 → 1+1 → 只取最强 1」逐级降配，**最后那一级把「读数最新」这个键
    整个丢掉了**——而在生产量级上它就是常态：合成 3,788 个候选、两个出发点，
    交叉表有 80 个非空格子，`sample_size=60` 恰好落在最后一级，样本里一个
    「最新」都没有。丢掉一个键 = 丢掉「不泄露答案」这个设计本身。

    这一版改成**按格子轮转填**：先给每个格子发第一个代表，还有余量再发第二个，
    以此类推。于是预算紧时先减少的是「每格的第 2、3 个」，**两个键始终都在**：
    格子之间轮流由「最强」和「最新」领头（偶数格最强领头、奇数格最新领头），
    所以哪怕每格只发得起一个，样本里两个键也各占约一半。

    `sample_size` 是**硬上限**，`targets` 绝不会超过它；覆盖不到的格子在摘要
    交叉表里仍然逐格列着个数与中位数，AI 看得见它们存在。
    """
    if sample_size < 1:
        raise ValueError("sample_size must be at least 1")

    cells: dict[tuple[Coordinate, int, str, str], list[ScoredTarget]] = {}
    for target in candidates:
        for origin in origins:
            cells.setdefault(_cell_key(origin, target), []).append(target)

    lanes: list[list[ScoredTarget]] = []
    for index, key in enumerate(sorted(cells)):
        items = cells[key]
        strong = sorted(items, key=_strongest_first)[:PER_CELL_PER_KEY]
        fresh = sorted(items, key=_freshest_first)[:PER_CELL_PER_KEY]
        leading, trailing = (strong, fresh) if index % 2 == 0 else (fresh, strong)
        lane: list[ScoredTarget] = []
        in_lane: set[Coordinate] = set()
        for depth in range(PER_CELL_PER_KEY):
            for source in (leading, trailing):
                if depth < len(source) and source[depth].coordinate not in in_lane:
                    in_lane.add(source[depth].coordinate)
                    lane.append(source[depth])
        lanes.append(lane)

    picked: list[ScoredTarget] = []
    seen: set[Coordinate] = set()
    per_lane = [0] * len(lanes)
    depth = 0
    while len(picked) < sample_size and any(len(lane) > depth for lane in lanes):
        for index, lane in enumerate(lanes):
            if len(picked) >= sample_size:
                break
            if depth >= len(lane) or lane[depth].coordinate in seen:
                continue
            seen.add(lane[depth].coordinate)
            picked.append(lane[depth])
            per_lane[index] += 1
        depth += 1
    return StratifiedSample(
        targets=tuple(picked),
        cells_total=len(lanes),
        cells_covered=sum(1 for count in per_lane if count),
        max_per_cell=max(per_lane) if per_lane else 0,
    )


def _age_distribution(candidates: Sequence[ScoredTarget], now: datetime) -> str:
    """全池读数龄的分布：中位 / p90 / 最大 / 有多少个根本没读数（方案 2.3）。

    ⚠️ **这一段以前只有一句「下面是全池读数龄分布」而后面什么都没有。**
    对模型说「有」而实际没有，比不说更糟：它会以为自己漏读了，或者干脆
    编一个分布出来。
    """
    ages = sorted(
        (now - target.military_score_at_utc).total_seconds() / 3600
        for target in candidates
        if target.military_score_at_utc is not None
    )
    missing = len(candidates) - len(ages)
    if not ages:
        return (
            f"- 全池读数龄分布：**{missing} 个候选一个军力读数都没有**"
            f"（全池 {len(candidates)} 个），算不出分布"
        )
    median = _median(ages)
    p90 = ages[min(len(ages) - 1, max(0, math.ceil(0.9 * len(ages)) - 1))]
    return (
        f"- 全池读数龄分布：中位 {median:.2f}h / p90 {p90:.2f}h / 最大 {ages[-1]:.2f}h；"
        f"其中 **{missing} 个候选根本没有读数**（全池 {len(candidates)} 个）"
    )


#: 输出格式的示例。⚠️ **坐标是 `EXAMPLE_*` 那两个占位值，不是任何真实星球。**
#: 这个常量抽出来是为了让「模板里没有真实坐标」这件事**能被用例断言**，
#: 而不只是靠人读一遍代码（用例见 `tests/unit/application/test_ai_shadow_prompt.py`）。
OUTPUT_EXAMPLE = (
    "{\n"
    '  "picks": [\n'
    f'    {{"target": "{EXAMPLE_TARGET}", "origin": "{EXAMPLE_ORIGIN}",'
    ' "preset": "<预设名>", "rank": 1,\n'
    '      "military": 12345, "reading_age_hours": 0.3, "round_trip_minutes": 125,\n'
    '      "reason": "为什么是它——引用上面给过的军力 / 读数龄 / 往返"}\n'
    "  ],\n"
    '  "pool_warnings": [\n'
    '    {"severity": "high", "finding": "……", "suggested_check": "……"}\n'
    "  ],\n"
    '  "confidence": "high|medium|low",\n'
    '  "notes": "一句话说清这一轮的主要取舍"\n'
    "}\n"
    "⚠️ 上面的坐标与数字是**占位示例**（0 号银河并不存在），照抄会被当场作废。"
)


def _algorithm_picks_json(assignments: Sequence[Any]) -> str:
    """把现有算法的选择序列化成 JSON 存进 `algorithm_picks_json`。"""
    return json.dumps(
        [
            {
                "target": (
                    f"{item.coordinate.galaxy}:{item.coordinate.system}:{item.coordinate.position}"
                ),
                "origin": f"{item.origin.galaxy}:{item.origin.system}:{item.origin.position}",
                "preset": item.preset,
            }
            for item in assignments
        ],
        ensure_ascii=False,
    )


def build_prompt(
    *,
    task_id: int,
    now: datetime,
    cycle_start: datetime,
    origins: Sequence[Coordinate],
    inflight_by_origin: Mapping[Coordinate, Sequence[InflightLine]],
    configured_lines: Mapping[Coordinate, int],
    budgets_by_origin: Mapping[Coordinate, int],
    account_limit: int | None,
    account_inflight: int,
    candidates: Sequence[ScoredTarget],
    presets: frozenset[str],
    last_attack_at: Mapping[Coordinate, datetime | None],
    protected_seen_at: Mapping[Coordinate, datetime | None],
    sample_size: int = DEFAULT_AI_SAMPLE_SIZE,
) -> str:
    """组装 prompt。**账号名 / 玩家名 / 单位数量 / 五个旋钮的值一律不出现在这里。**

    ⚠️ **`candidates` 是选靶第 1 步之后的全池，不是 `eligible`。** 喂 `eligible`
    等于把答案先塞给它：那一批已经过了有效期（读数窗口）、窗口门限和军力上限
    三道旋钮（前两道 2026-08-23 起住在 `military_attack_config`，第三道仍是任务
    参数 `max_score`），**旋钮的数值没进
    prompt，筛选效果却原样加在它能看到的池子上**——而这几个旋钮恰恰是这一期
    要让 AI 去替代的东西（方案第一节）。

    `last_attack_at` / `protected_seen_at` 用来给每个样本行标「我方上次攻击距今
    多久」「是否撞过保护期及时刻」，这是它判断保护期与攻击间隔的原始事实。
    ⚠️ `last_attack_at` 必须由 `attack_dispatches` 算出来——
    `bot_targets.last_attack_at_utc` 那一列从来没被写过。

    `sample_size` 是那个运维旋钮（`military_attack_config.ai_sample_size`）。
    """
    total_budget = sum(budgets_by_origin.values())
    budget_text = _budget_section(
        origins=origins,
        inflight_by_origin=inflight_by_origin,
        configured_lines=configured_lines,
        budgets_by_origin=budgets_by_origin,
        account_limit=account_limit,
        account_inflight=account_inflight,
    )
    summary_text, samples_text, sampling_note = _pool_sections(
        candidates=candidates,
        origins=origins,
        now=now,
        last_attack_at=last_attack_at,
        protected_seen_at=protected_seen_at,
        sample_size=sample_size,
    )
    presets_text = "、".join(sorted(presets)) if presets else BOT_ATTACK_PRESET
    sections = [
        (
            "你是《永恒虚空》网页游戏的选靶参谋。当前处于**影子模式**：你的选择"
            "不会被执行，只用于事后对比。请根据下面的局面，从候选中选出恰好 "
            f"{total_budget} 个本轮该打的目标。"
        ),
        "# 一、你现在的位置与航线（★ 这决定本轮能打几发）",
        budget_text,
        (
            f"★ 本轮真实可派发数 = 各出发星球空闲航线之和，与全账号余量取小 = "
            f"**{total_budget}**。\n"
            "你的 picks 必须**恰好**返回这么多个目标——情报每小时贬值，空等只会更差。"
        ),
        "# 二、候选池（⚠️ 这是分层抽样，不是全池）",
        summary_text,
        f"{sampling_note}\n{samples_text}",
        "# 三、时间",
        f"- 本周期起点（军力每周一 UTC+0 刷新）：{cycle_start:%Y-%m-%d %H:%M} UTC",
        (
            "- 现在是刷新后第 "
            f"{int((now - cycle_start).total_seconds() // 86400)} 天、第 "
            f"{int((now - cycle_start).total_seconds() // 3600)} 小时\n"
            "- 每个样本自己的读数龄（小时）标在上面那张样本表里。读数越旧越不可信。"
        ),
        _age_distribution(candidates, now),
        "# 四、飞行时间公式（可用来估算任意候选的往返）",
        (f"- 同银河：D = 1162 + 31.71 × 恒星系环距（恒星系首尾相接，{SYSTEMS_PER_GALAXY} 环）"),
        f"- 跨银河：D = 20000 × 银河环距（银河 {TOTAL_GALAXIES} 环）",
        "- 单程秒 = 2 + k × √D（k 按出发星球标定）；往返 = 单程 × 2",
        (
            "- ⚠️ 适用域：同银河近距离一档 p90 误差 32–50%"
            "（D 常数在近距离本身就不准）；\n"
            "  跨银河一档最大 0.03%。**两档不要一视同仁。**"
        ),
        "# 五、军力与收益的关系（实测拟合，⚠️ 注意证据强度）",
        "核心三资源 = 稀有三样（合金碎片 / 泰坦立方 / 收割者碎片），收益模型：",
        "    稀有三样 ≈ 0.141 × 军力 × exp(−0.068 × 快照龄小时)",
        (
            "- n = 74，R² = 0.781，样本 2026-08-18~19。衰减 = 每小时 6.6%，"
            "半衰期 10.2 小时，6 小时后剩 66%。"
        ),
        (
            "- ⚠️ **基础三样（金属/晶体/气体）绝不能用来判断目标价值**：实测它由"
            "我方货舱容量决定，不由目标决定。"
        ),
        (
            "- ⚠️ 证据强度：收益曲线是**实测**（n=74）；「军力高 = 被打得少」是**推断**；\n"
            "  「周内相位（周一竞争最激烈…）」是**领域直觉、零数据支撑**——不要当事实用。"
        ),
        "# 六、游戏规则与我方能力",
        (
            "- 保护期：任何人打过触发 **8 小时**，且只有撞上才知道。"
            "我方已知撞过保护期的坐标标在样本里。"
        ),
        "- 我方每个坐标上次攻击时刻已标在样本里（保护期内不要选）。",
        f"- 可用预设：{presets_text}。选择你认为该目标量级配得上的那一个。",
        ("- 每发大约 38–42 秒鼠标开销（撞保护期也一样）——这是航线之外的第二种成本。"),
        "# 七、输出（严格 JSON，不要输出任何多余文字）",
        OUTPUT_EXAMPLE,
        "要求：",
        f"1. picks **恰好** {total_budget} 个，不多不少。",
        "2. target / origin / preset 只能来自上面给过的集合。",
        "3. 同一坐标不许出现两次。",
        (
            "4. **必须把 military / reading_age_hours / round_trip_minutes 三个数字填回**"
            "——我们会拿自己的数据逐项核对，编数字会被检出。"
        ),
    ]
    return "\n\n".join(sections)


def _budget_section(
    *,
    origins: Sequence[Coordinate],
    inflight_by_origin: Mapping[Coordinate, Sequence[InflightLine]],
    configured_lines: Mapping[Coordinate, int],
    budgets_by_origin: Mapping[Coordinate, int],
    account_limit: int | None,
    account_inflight: int,
) -> str:
    lines: list[str] = []
    for origin in origins:
        inflight = list(inflight_by_origin.get(origin, ()))
        unknown = [line for line in inflight if line.line_free_at_utc is None]
        known = [line for line in inflight if line.line_free_at_utc is not None]
        free = budgets_by_origin.get(origin, 0)
        line = (
            f"出发星球 {origin}\n"
            f"  配置航线 {configured_lines.get(origin, 0)}\n"
            f"  在飞 {len(inflight)}（其中 {len(unknown)} 条时长未知，见下）\n"
            f"  ⇒ 此刻空闲 {'0' if free <= 0 else free}"
        )
        if known:
            when = " / ".join(f"{item.line_free_at_utc:%H:%M:%S}" for item in known)
            line += f"\n  已占用各自最早空出：{when}"
        if unknown:
            line += (
                f"\n  ⚠️ 时长未知的 {len(unknown)} 条按兜底 90 分钟占着，"
                "实际可能早回来了——别把它当成「这条线还要等很久」的确定性证据"
            )
        lines.append(line)
    if account_limit is not None:
        lines.append(
            f"\n全账号航线上限 {account_limit}，已在飞 {account_inflight}，"
            f"全账号剩余 {max(account_limit - account_inflight, 0)}"
        )
    else:
        lines.append("\n未配置全账号航线上限（那道闸不生效）。")
    return "\n".join(lines)


def _pool_sections(
    candidates: Sequence[ScoredTarget],
    origins: Sequence[Coordinate],
    *,
    now: datetime,
    last_attack_at: Mapping[Coordinate, datetime | None],
    protected_seen_at: Mapping[Coordinate, datetime | None],
    sample_size: int = DEFAULT_AI_SAMPLE_SIZE,
) -> tuple[str, str, str]:
    """候选池的交叉表摘要 + 样本清单。返回 (summary, samples_text, sampling_note)。

    交叉表按**每个出发点各一份**：往返时间是 (目标, 出发星球) 的函数，同一目标
    从不同星出发落在不同往返档里。

    `sampling_note` 说的是**这一次实际怎么抽的**——每格取了几个、覆盖了几个格子。
    ⚠️ 不许写死「每格 3+3」：抽样会按预算降配，写死就是对模型说假话。
    """
    cells: dict[tuple[Coordinate, int, str, str], list[ScoredTarget]] = {}
    for target in candidates:
        for origin in origins:
            cells.setdefault(_cell_key(origin, target), []).append(target)

    summary_lines: list[str] = []
    for origin in origins:
        own = {key: items for key, items in cells.items() if key[0] == origin}
        if not own:
            summary_lines.append(f"出发点 {origin}：候选池里没有目标")
            continue
        summary_lines.append(f"出发点 {origin}：候选 {sum(len(v) for v in own)} 个")
        for (_, galaxy, trip, score), items in sorted(own.items()):
            scores = [item.military_score for item in items if item.military_score is not None]
            ages = [
                item.military_score_at_utc
                for item in items
                if item.military_score_at_utc is not None
            ]
            median_score = _median(scores) if scores else None
            median_age = _median_hours(ages, now) if ages else None
            score_text = "—" if median_score is None else f"{median_score:,.0f}"
            age_text = "—" if median_age is None else f"{median_age:.2f}h"
            summary_lines.append(
                f"    银河 {galaxy} | 往返 {trip} | 军力 {score}: {len(items)} 个，"
                f"军力中位 {score_text}，龄中位 {age_text}"
            )

    sample = stratified_samples(candidates, origins, sample_size=sample_size)
    sample_lines: list[str] = []
    for target in sample.targets:
        trip_text = " / ".join(
            f"从{origin} {round_trip_hours(target.coordinate, origin) * 60:.0f}分"
            for origin in origins
        )
        age = target.military_score_at_utc
        age_text = "—" if age is None else f"{(now - age).total_seconds() / 3600:.2f}h"
        last = last_attack_at.get(target.coordinate)
        last_text = (
            "我方从未打过"
            if last is None
            else f"我方上次攻击距今 {(now - last).total_seconds() / 3600:.1f}h"
        )
        protected = protected_seen_at.get(target.coordinate)
        protected_text = "未知" if protected is None else f"已知撞过保护期（{protected:%H:%M} UTC）"
        score_text = (
            "无读数（军力榜没见过它，不是「很弱」）"
            if target.military_score is None
            else f"{target.military_score:,.0f}"
        )
        sample_lines.append(
            f"    {target.coordinate} | 军力 {score_text} | 龄 {age_text} | "
            f"往返 {trip_text} | {last_text} | 保护期 {protected_text}"
        )
    note = (
        f"样本（分层抽样，**不是全池**：每个非空格子按「军力最高」与「读数最新」"
        f"两个键各取至多 {PER_CELL_PER_KEY} 个，格子之间轮流由「最强」和「最新」领头，"
        f"再按格子轮转填到上限。\n"
        f"本次实取 {len(sample.targets)} 个，覆盖 {sample.cells_covered}/{sample.cells_total} "
        f"个非空格子，单格最多 {sample.max_per_cell} 个；全池共 {len(candidates)} 个候选。\n"
        f"⚠️ 样本之外的候选我们没有给出坐标，所以这一轮只能从下面这张表里选；"
        f"但上面那张交叉表给的是**全池真实总数**——样本数不等于可选目标数，"
        f"发现某一格明显被抽样漏掉了，写进 pool_warnings。）："
    )
    return "\n".join(summary_lines), "\n".join(sample_lines), note


class AiShadowObserver:
    """挂在 `MissionScheduler` 上的影子观测器。

    调用方（调度器）在 `_military_assignments` 返回前调 `observe()`——同步、
    非阻塞：全部重活在后台线程，线程有上限、每任务有限流。
    """

    def __init__(
        self,
        repository: SqlAlchemyRepository,
        settings: Settings,
        *,
        clock: Any = None,
        monotonic: Any = None,
        sample_size: int | None = None,
        timeout_s: float | None = None,
        model: str | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._clock = clock or _utc_now
        self._monotonic = monotonic or time.monotonic
        self._sample_size = sample_size if sample_size is not None else DEFAULT_AI_SAMPLE_SIZE
        self._timeout_s = timeout_s if timeout_s is not None else DEFAULT_AI_TIMEOUT_S
        #: 模型名。显式注入优先（测试用），否则读 Settings 里用户配的那份，
        #: 再退回代码默认（空）。
        self._model = model if model is not None else (settings.ai_model or "")
        self._lock = threading.Lock()
        self._active = 0
        self._last_request_at: dict[int, float] = {}
        #: 是否真能发请求。缺 httpx 时整条观测降级为不可用。
        self._httpx = httpx
        self._disabled_reason = None if httpx is not None else "httpx 未安装"
        #: 上一次记过的「可用 / 不可用」。None = 还没记过任何一条。
        #: **只在跃迁时写日志**，同 `mission_scheduler._log_a_repeated_line` 的规矩。
        self._availability: bool | None = None
        #: 各种「这一下跳过了」上一次落库的单调时刻，按 `SKIP_LOG_THROTTLE_S` 限流。
        self._skip_logged_at: dict[str, float] = {}

    # -- 供调度器 --------------------------------------------------------------

    @property
    def available(self) -> bool:
        """**凭据 + 依赖**齐了没有。⚠️ **它不看开关。**

        开关（`military_attack_config.ai_shadow_enabled`）由调度器一侧判
        （`MissionScheduler._ai_shadow_enabled`），因为「开关关掉时不组 prompt、
        不起线程」这条要在调用之前就成立。`observe()` 自己也会再确认一次开关
        （用的是 `_read_knobs` 本来就要读的那一行，不多花一次查询）——
        **这里返回 True 不代表这一轮会真的发请求。**
        """
        if self._disabled_reason is not None:
            return False
        return bool(self._settings.ai_api_base and self._settings.ai_api_key)

    #: 旧名。`available` 才说得准它管的是什么（不含开关），保留这个别名是因为
    #: 「observer.enabled」读起来太像「整条功能开着」，正是要避免的误解。
    @property
    def enabled(self) -> bool:
        """⚠️ **已改名为 `available`**：它只看凭据与依赖，**不看开关**。"""
        return self.available

    def _note_availability(self, available: bool, reason: str) -> None:
        """「可用 ↔ 不可用」的跃迁记一条。**只在变化时写**，不受限流窗口约束。

        ⚠️ 这条是补 2026-08-19 审查发现的那个洞：用户把开关打开、`.env` 少一个键
        或者 `httpx` 没装，**页面上什么都不会发生，而库里一个字都查不到原因**。
        这正是 CLAUDE.md 里「日志不说话把故障拖了两天」的复发形态。
        """
        if self._availability is available:
            return
        first_time = self._availability is None
        self._availability = available
        if first_time and available:
            # 第一轮就一切正常：没什么可说的。「恢复可用」只在真的**恢复**时才写，
            # 否则每次进程重启都会多出一条不带信息的 INFO。
            return
        record_system_log(
            "INFO" if available else "WARNING",
            "application.ai_targeting",
            (
                "AI 选靶影子：观测恢复可用（凭据与依赖都齐了）"
                if available
                else f"AI 选靶影子：开关开着但观测不可用——{reason}；这一轮不会发起任何调用"
            ),
            payload={"available": available, "reason": None if available else reason},
        )

    def _note_skip(self, code: str, message: str, payload: dict[str, Any], at: float) -> None:
        """「这一下跳过了」的限流日志。同一个 `code` 每 `SKIP_LOG_THROTTLE_S` 一条。

        ⚠️ 时刻由调用方传进来，**不在这里再读一次单调钟**：`observe()` 一次只该
        取一次时刻，注入假钟的用例才数得清。
        """
        last = self._skip_logged_at.get(code)
        if last is not None and at - last < SKIP_LOG_THROTTLE_S:
            return
        self._skip_logged_at[code] = at
        record_system_log(
            "INFO",
            "application.ai_targeting",
            message,
            payload={**payload, "skip_code": code},
        )

    def observe(
        self,
        *,
        task_id: int,
        now: datetime,
        run_id: UUID | None,
        budget: int,
        candidates: Sequence[ScoredTarget],
        origins: Sequence[Coordinate],
        configured_lines: Mapping[Coordinate, int],
        budgets_by_origin: Mapping[Coordinate, int],
        account_inflight: int,
        account_limit: int | None,
        hold: timedelta,
        presets: frozenset[str],
        assignments: Sequence[Any],
    ) -> bool:
        """同步入口。返回「这一下真的发起了一个观测」。

        开关关掉、没有凭据、池子为空、线程已满、该任务还在冷却内，都返回 False
        且**不发起任何调用**（需求第八节）。

        ⚠️ **每一条返回 False 的路都要在库里留下痕迹**（限流之后）：用户把开关
        打开却什么都不发生，必须能从 `system_log` 里查出是哪一条把它挡掉的。
        唯一不记的是 `budget < 1` / 池子为空——那是「这一轮本来就没活干」，
        调度器自己的日志已经说清楚了。
        """
        if not self.available:
            self._note_availability(False, self._disabled_reason or "缺少 AI API 凭据")
            return False
        self._note_availability(True, "")
        if budget < 1:
            return False
        if not candidates:
            return False
        # 凭据已确认齐了，才读那一行配置（开关 + 三个行为旋钮，一次查询全拿到）。
        # ⚠️ 开关在这里**再确认一次**：调度器一侧已经判过，但 observer 不该把
        # 「调用方一定判过」当成自己的保险——多一个调用点就会破。
        sample_size, timeout_s, model, switch_on = self._read_knobs()
        with self._lock:
            now_m = self._monotonic()
            if not switch_on:
                self._note_skip(
                    "switch_off",
                    "AI 选靶影子：开关（military_attack_config.ai_shadow_enabled）没开，跳过",
                    {"task_id": task_id},
                    now_m,
                )
                return False
            if self._active >= MAX_CONCURRENT_WORKERS:
                self._note_skip(
                    "workers_busy",
                    f"AI 选靶影子：已有 {self._active} 个观测线程在跑"
                    f"（上限 {MAX_CONCURRENT_WORKERS}），任务 {task_id} 这一轮跳过",
                    {"task_id": task_id, "active": self._active},
                    now_m,
                )
                return False
            if now_m - self._last_request_at.get(task_id, float("-inf")) < AI_SHADOW_MIN_INTERVAL_S:
                self._note_skip(
                    f"throttled:{task_id}",
                    f"AI 选靶影子：任务 {task_id} 距上次发起不足 "
                    f"{AI_SHADOW_MIN_INTERVAL_S:.0f} 秒，这一轮跳过（限流，不是故障）",
                    {"task_id": task_id, "min_interval_s": AI_SHADOW_MIN_INTERVAL_S},
                    now_m,
                )
                return False
            self._last_request_at[task_id] = now_m
            self._active += 1
        threading.Thread(
            target=self._worker,
            args=(
                task_id,
                now,
                run_id,
                budget,
                tuple(candidates),
                tuple(origins),
                dict(configured_lines),
                dict(budgets_by_origin),
                account_inflight,
                account_limit,
                hold,
                frozenset(presets),
                tuple(assignments),
                sample_size,
                timeout_s,
                model,
            ),
            name=f"ai-shadow-{task_id}",
            daemon=True,
        ).start()
        return True

    def _read_knobs(self) -> tuple[int, float, str, bool]:
        """一次查询读出开关与三个行为旋钮（改动即生效）。

        返回 `(sample_size, timeout_s, model, switch_on)`。⚠️ **开关也从这里读**：
        调度器一侧已经判过一次，但那是调用方的事；observer 自己再确认一次，
        用的是本来就要读的同一行，**不多花一次查询**。

        读不到（表没初始化 / 配置行不存在）就回落 `__init__` 里的默认值，
        **开关按「关」处理**——没配置行时宁可什么都不发。
        """
        sample_size = self._sample_size
        timeout_s = self._timeout_s
        model = self._model
        try:
            row = self._repository.military_attack_config()
        except ValueError:
            return sample_size, timeout_s, model, False
        if row.ai_sample_size is not None:
            sample_size = int(row.ai_sample_size)
        if row.ai_timeout_seconds is not None:
            timeout_s = float(row.ai_timeout_seconds)
        if row.ai_model:
            model = row.ai_model
        return sample_size, timeout_s, model, bool(row.ai_shadow_enabled)

    def _worker(self, *args: Any) -> None:
        try:
            self._work(*args)
        except Exception as error:  # noqa: BLE001 - 任何异常都不许让线程死而无痕
            record_system_log(
                "ERROR",
                "application.ai_targeting",
                f"AI 选靶影子线程异常（任务 {args[0]}）：{error!r}",
                payload={"task_id": args[0]},
            )
        finally:
            with self._lock:
                self._active -= 1

    def _work(
        self,
        task_id: int,
        now: datetime,
        run_id: UUID | None,
        budget: int,
        candidates: tuple[ScoredTarget, ...],
        origins: tuple[Coordinate, ...],
        configured_lines: dict[Coordinate, int],
        budgets_by_origin: dict[Coordinate, int],
        account_inflight: int,
        account_limit: int | None,
        hold: timedelta,
        presets: frozenset[str],
        assignments: tuple[Any, ...],
        sample_size: int,
        timeout_s: float,
        model: str,
    ) -> None:
        cycle_start = cycle_start_utc(now)
        # 这些查询发生在线程里：主 tick 不因为一次 LLM 往返等这些查询。
        inflight: dict[Coordinate, list[InflightLine]] = {}
        for origin in origins:
            inflight[origin] = self._repository.inflight_lines(
                now_utc=now, origin=origin, hold=hold
            )
        # ⚠️ 这两份事实要覆盖**整个候选池**，不能只覆盖被抽到的样本：硬校验允许
        # AI 从全池里挑，软核对（保护期 / 距上次攻击 8 小时）就得对全池都答得出来。
        # 池子有三四千个，两个查询在仓库里已按块拆开发（见 `last_bot_attack_at`）。
        pool_coordinates = [item.coordinate for item in candidates]
        last_attack = self._repository.last_bot_attack_at(pool_coordinates)
        protected = self._repository.bot_target_protection_seen_at(pool_coordinates)

        prompt = build_prompt(
            task_id=task_id,
            now=now,
            cycle_start=cycle_start,
            origins=origins,
            inflight_by_origin=inflight,
            configured_lines=configured_lines,
            budgets_by_origin=budgets_by_origin,
            account_limit=account_limit,
            account_inflight=account_inflight,
            candidates=candidates,
            presets=presets,
            last_attack_at=last_attack,
            protected_seen_at=protected,
            sample_size=sample_size,
        )
        algorithm_picks_json = _algorithm_picks_json(assignments)
        reference = _soft_reference(candidates, origins, last_attack, protected, now)

        started = time.monotonic()
        response_text: str | None
        status: str
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        try:
            response_text, prompt_tokens, completion_tokens = self._call_llm(
                prompt, timeout_s=timeout_s, model=model
            )
            status = AiDecisionStatus.OK.value
        except TimeoutError:
            response_text, status = None, AiDecisionStatus.TIMEOUT.value
        except Exception as error:  # noqa: BLE001
            # httpx 的超时不是内置 `TimeoutError`，得按它的类型认。
            if self._httpx is not None and isinstance(error, self._httpx.TimeoutException):
                response_text, status = None, AiDecisionStatus.TIMEOUT.value
            else:
                response_text, status = None, AiDecisionStatus.HTTP_ERROR.value
                record_system_log(
                    "WARNING",
                    "application.ai_targeting",
                    f"AI 选靶影子：任务 {task_id} 调用失败 {error!r}；本轮照常按算法派遣",
                    payload={"task_id": task_id, "status": status},
                )
        latency_ms = int((time.monotonic() - started) * 1000)

        violations: list[dict[str, object]] = []
        ai_picks_json: str | None = None
        overlap: int | None = None
        if status == AiDecisionStatus.OK.value and response_text is not None:
            status, violations, ai_picks_json, overlap = self._decode_and_check(
                response_text,
                candidates,
                origins,
                budgets_by_origin,
                budget,
                assignments,
                reference,
            )

        record = AiTargetDecision(
            decided_at_utc=now,
            task_id=task_id,
            run_id=run_id,
            cycle_start_utc=cycle_start,
            budget=budget,
            algorithm_picks_json=algorithm_picks_json,
            ai_picks_json=ai_picks_json,
            overlap=overlap,
            prompt_text=prompt,
            response_text=response_text,
            model=model or None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            status=status,
            violations_json=json.dumps(violations, ensure_ascii=False) if violations else "[]",
        )
        try:
            self._repository.save_ai_target_decision(record)
        except Exception as error:  # noqa: BLE001 - 落库失败只记日志，不连锁
            record_system_log(
                "ERROR",
                "application.ai_targeting",
                f"AI 选靶影子记录落库失败（任务 {task_id}）：{error!r}",
                payload={"task_id": task_id, "status": status},
            )
            return
        message = f"AI 选靶影子：任务 {task_id} 记录为 {status}" + (
            f"（重合 {overlap}/{budget}，延迟 {latency_ms}ms，软核对 {len(violations)} 条）"
            if status == AiDecisionStatus.OK.value
            else f"（预算 {budget}）"
        )
        record_system_log(
            "WARNING" if status != AiDecisionStatus.OK.value else "INFO",
            "application.ai_targeting",
            message,
            payload={
                "task_id": task_id,
                "status": status,
                "budget": budget,
                "overlap": overlap,
                "latency_ms": latency_ms,
                "model": self._model or None,
            },
        )

    def _call_llm(
        self, prompt: str, *, timeout_s: float, model: str
    ) -> tuple[str, int | None, int | None]:
        """OpenAI 兼容的 `POST {base}/chat/completions`。返回 (text, pt, ct)。"""
        if self._httpx is None:
            raise RuntimeError("httpx 未安装，AI 影子观测不可用")
        if not self._settings.ai_api_base or not self._settings.ai_api_key:
            raise RuntimeError("缺少 AI API 凭据（EVO_HELPER_API / EVO_HELPER_API_key）")
        effective_model = model or self._settings.ai_model or ""
        response = self._httpx.post(
            self._settings.ai_api_base,
            headers={"Authorization": f"Bearer {self._settings.ai_api_key}"},
            json={
                "model": effective_model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": AI_TEMPERATURE,
                "max_tokens": AI_MAX_TOKENS,
            },
            timeout=timeout_s,
        )
        response.raise_for_status()
        data = response.json()
        try:
            text = str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError(f"模型响应缺少 choices[0].message.content：{error!r}") from error
        usage = data.get("usage") or {}
        return (
            text,
            _optional_int(usage.get("prompt_tokens")),
            _optional_int(usage.get("completion_tokens")),
        )

    def _decode_and_check(
        self,
        response_text: str,
        candidates: tuple[ScoredTarget, ...],
        origins: tuple[Coordinate, ...],
        budgets_by_origin: dict[Coordinate, int],
        budget: int,
        assignments: tuple[Any, ...],
        reference: SoftReference,
    ) -> tuple[str, list[dict[str, object]], str | None, int | None]:
        """解析响应并跑硬校验 + 软核对。返回 (status, violations, ai_picks_json, overlap)。

        硬校验不过（violations 非空）时 `ai_picks_json` 为 None（整份作废），
        `status` 记为 `schema_violation`。
        """
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as error:
            return (
                AiDecisionStatus.INVALID_JSON.value,
                [{"code": "invalid_json", "detail": str(error)}],
                None,
                None,
            )
        if not isinstance(data, dict):
            return (
                AiDecisionStatus.INVALID_JSON.value,
                [{"code": "invalid_json", "detail": "响应不是 JSON 对象"}],
                None,
                None,
            )
        raw_picks = data.get("picks")
        if not isinstance(raw_picks, list):
            return (
                AiDecisionStatus.SCHEMA_VIOLATION.value,
                [{"code": "missing_picks", "detail": "响应里没有 picks 数组"}],
                None,
                None,
            )
        picks: list[AiPick] = []
        for index, item in enumerate(raw_picks):
            parsed = parse_pick(item)
            if parsed is None:
                return (
                    AiDecisionStatus.SCHEMA_VIOLATION.value,
                    [{"code": "malformed_pick", "detail": f"第 {index + 1} 个 pick 形状不对"}],
                    None,
                    None,
                )
            picks.append(parsed)

        vocabulary = PickVocabulary(
            targets=frozenset(item.coordinate for item in candidates),
            origins=frozenset(origins),
            presets=_preset_names(assignments),
            budget_by_origin=budgets_by_origin,
            total_budget=budget,
        )
        algorithm_targets = {item.coordinate for item in assignments}
        violations, overlap = validate_picks(picks, vocabulary, algorithm_targets)

        ai_picks_json: str | None = None
        if not violations:
            ai_picks_json = json.dumps([_pick_to_dict(pick) for pick in picks], ensure_ascii=False)
            violations.extend(soft_check_picks(picks, reference))
            return (
                AiDecisionStatus.OK.value,
                violations,
                ai_picks_json,
                overlap,
            )
        return (
            AiDecisionStatus.SCHEMA_VIOLATION.value,
            violations,
            None,
            None,
        )


def _soft_reference(
    candidates: tuple[ScoredTarget, ...],
    origins: tuple[Coordinate, ...],
    last_attack_at: Mapping[Coordinate, datetime | None],
    protected_seen_at: Mapping[Coordinate, datetime | None],
    now: datetime,
) -> SoftReference:
    """软核对要的我方事实。`last_attack_at` / `protected_seen_at` 在线程里查好传入。

    ⚠️ **全池里有一批根本没有军力读数**（`candidates` 不像 `eligible` 那样要求
    有读数），它们要单独记一份：只把它们从 `military` 里漏掉的话，AI 给它们
    编一个军力数会被当成「无从核对」放过去。
    """
    military: dict[Coordinate, float] = {}
    ages: dict[Coordinate, float] = {}
    without_reading: set[Coordinate] = set()
    for item in candidates:
        if item.military_score is None:
            without_reading.add(item.coordinate)
        else:
            military[item.coordinate] = item.military_score
        if item.military_score_at_utc is not None:
            ages[item.coordinate] = (now - item.military_score_at_utc).total_seconds() / 3600
    round_trip: dict[Coordinate, dict[Coordinate, float]] = {
        item.coordinate: {
            origin: round_trip_hours(item.coordinate, origin) * 60 for origin in origins
        }
        for item in candidates
    }
    protected_until: dict[Coordinate, datetime | None] = {
        coordinate: (None if seen_at is None else seen_at + timedelta(hours=GAME_PROTECTION_HOURS))
        for coordinate, seen_at in protected_seen_at.items()
    }
    return SoftReference(
        military=military,
        reading_age_hours=ages,
        round_trip_minutes=round_trip,
        last_attack_at=last_attack_at,
        protected_until=protected_until,
        now=now,
        targets_without_reading=frozenset(without_reading),
    )


def _pick_to_dict(pick: AiPick) -> dict[str, object]:
    return {
        "target": f"{pick.target.galaxy}:{pick.target.system}:{pick.target.position}",
        "origin": f"{pick.origin.galaxy}:{pick.origin.system}:{pick.origin.position}",
        "preset": pick.preset,
        "rank": pick.rank,
        "military": pick.military,
        "reading_age_hours": pick.reading_age_hours,
        "round_trip_minutes": pick.round_trip_minutes,
        "reason": pick.reason,
    }


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _preset_names(assignments: Sequence[Any]) -> frozenset[str]:
    """AI 只能从我们本轮实际用到的预设里选——从算法选中的目标上提取。"""
    return frozenset(item.preset for item in assignments)


__all__ = [
    "AI_MAX_TOKENS",
    "AI_SHADOW_MIN_INTERVAL_S",
    "AI_TEMPERATURE",
    "AiShadowObserver",
    "DEFAULT_AI_RETENTION_DAYS",
    "DEFAULT_AI_SAMPLE_SIZE",
    "DEFAULT_AI_TIMEOUT_S",
    "EXAMPLE_ORIGIN",
    "EXAMPLE_TARGET",
    "NO_READING_BUCKET",
    "OUTPUT_EXAMPLE",
    "PER_CELL_PER_KEY",
    "SKIP_LOG_THROTTLE_S",
    "StratifiedSample",
    "build_prompt",
    "stratified_samples",
]
