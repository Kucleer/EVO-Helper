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
import threading
import time
from collections.abc import Mapping, Sequence
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

#: 默认样本量。需求文档第七节：默认 60。
DEFAULT_AI_SAMPLE_SIZE = 60

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


def _score_bucket(score: float | None) -> str:
    """军力分档（prompt 里的交叉表用）。**只是描述性分桶，不是选靶旋钮。**"""
    value = score or 0.0
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


def stratified_samples(
    eligible: Sequence[ScoredTarget],
    origins: Sequence[Coordinate],
    *,
    sample_size: int,
) -> list[ScoredTarget]:
    """从 `eligible` 里取分层样本：每个 (出发点, 银河, 往返档, 军力档) 格子里
    军力最高 + 读数最新各若干，去重，总量钳到 `sample_size`。

    ⚠️ **抽样键刻意是「最强 / 最新」两个，不是现有得分。** 按 `军力 ÷ 往返`
    排序后取前 N 等于把要验证的那条公式的答案泄露给它（需求 4.2）。
    两个键都不是那条公式。

    `sample_size` 不足时逐级降配：每格 3+3 → 1+1 → 只取最强 1，仍超就按
    首次出现顺序截断。降配保证**每个非空格子至少有一个代表**。
    """
    if sample_size < 1:
        raise ValueError("sample_size must be at least 1")

    def collect(strong: int, fresh: int) -> list[ScoredTarget]:
        cells: dict[tuple[Coordinate, int, str, str], list[ScoredTarget]] = {}
        for target in eligible:
            for origin in origins:
                cells.setdefault(_cell_key(origin, target), []).append(target)
        picked: list[ScoredTarget] = []
        seen: set[Coordinate] = set()
        for items in cells.values():
            ordered: list[ScoredTarget] = []
            if strong:
                ordered += sorted(
                    items,
                    key=lambda t: (
                        t.military_score is None,
                        -(t.military_score or 0.0),
                        t.coordinate,
                    ),
                )[:strong]
            if fresh:
                ordered += sorted(
                    items,
                    key=lambda t: (
                        t.military_score_at_utc is None,
                        t.military_score_at_utc or _MIN_AGE_UTC,
                        t.coordinate,
                    ),
                )[:fresh]
            for item in ordered:
                if item.coordinate not in seen:
                    seen.add(item.coordinate)
                    picked.append(item)
        return picked

    # 逐级降配。**重新收集而不是在上一批上截断**：截断会让排在后面的格子
    # 整个没有代表。
    for strong, fresh in ((3, 3), (1, 1), (1, 0)):
        result = collect(strong, fresh)
        if len(result) <= sample_size:
            return result
    return collect(1, 0)[:sample_size]


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
    eligible: Sequence[ScoredTarget],
    presets: frozenset[str],
    last_attack_at: Mapping[Coordinate, datetime | None],
    protected_seen_at: Mapping[Coordinate, datetime | None],
    sample_size: int = DEFAULT_AI_SAMPLE_SIZE,
) -> str:
    """组装 prompt。**账号名 / 玩家名 / 单位数量 / 五个旋钮的值一律不出现在这里。**

    `last_attack_at` / `protected_seen_at` 只用来标样本行，让 AI 别去挑
    保护期里 / 刚打过的目标。`sample_size` 是那个运维旋钮
    （`military_attack_config.ai_sample_size`，默认 60）。
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
    summary_text, samples_text, sample_count, pool_total = _pool_sections(
        eligible=eligible,
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
        (
            "样本（每个非空格子取「军力最高 3 个 + 读数最新 3 个」，去重，共 "
            f"{sample_count} 个；全池 {pool_total} 个）：\n{samples_text}"
        ),
        "# 三、时间",
        f"- 本周期起点（军力每周一 UTC+0 刷新）：{cycle_start:%Y-%m-%d %H:%M} UTC",
        (
            "- 现在是刷新后第 "
            f"{int((now - cycle_start).total_seconds() // 86400)} 天、第 "
            f"{int((now - cycle_start).total_seconds() // 3600)} 小时\n"
            "- 下面是每个样本自己的读数龄（小时）与全池读数龄分布。读数越旧越不可信。"
        ),
        "# 四、飞行时间公式（可用来估算任意候选的往返）",
        (
            "- 同银河：D = 1162 + 31.71 × 恒星系环距"
            f"（恒星系首尾相接，{SYSTEMS_PER_GALAXY} 环）"
        ),
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
        (
            "- 每发大约 38–42 秒鼠标开销（撞保护期也一样）——"
            "这是航线之外的第二种成本。"
        ),
        "# 七、输出（严格 JSON，不要输出任何多余文字）",
        (
            "{\n"
            '  "picks": [\n'
            '    {"target": "3:98:12", "origin": "4:277:15", "preset": "BBB", "rank": 1,\n'
            '      "military": 31820, "reading_age_hours": 0.3, "round_trip_minutes": 125,\n'
            '      "reason": "军力 31,820、读数龄 0.3h、往返 125 分；折算后是本轮最高"}\n'
            '  ],\n'
            '  "pool_warnings": [\n'
            '    {"severity": "high", "finding": "……", "suggested_check": "……"}\n'
            "  ],\n"
            '  "confidence": "high|medium|low",\n'
            '  "notes": "一句话说清这一轮的主要取舍"\n'
            "}"
        ),
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
    eligible: Sequence[ScoredTarget],
    origins: Sequence[Coordinate],
    *,
    now: datetime,
    last_attack_at: Mapping[Coordinate, datetime | None],
    protected_seen_at: Mapping[Coordinate, datetime | None],
    sample_size: int = DEFAULT_AI_SAMPLE_SIZE,
) -> tuple[str, str, int, int]:
    """候选池的交叉表摘要 + 样本清单。返回 (summary, samples_text, sample_count, pool_total)。

    交叉表按**每个出发点各一份**：往返时间是 (目标, 出发星球) 的函数，同一目标
    从不同星出发落在不同往返档里。
    """
    cells: dict[tuple[Coordinate, int, str, str], list[ScoredTarget]] = {}
    for target in eligible:
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

    samples = stratified_samples(eligible, origins, sample_size=sample_size)
    sample_lines: list[str] = []
    for target in samples:
        trip_text = " / ".join(
            f"从{origin} {round_trip_hours(target.coordinate, origin) * 60:.0f}分"
            for origin in origins
        )
        age = target.military_score_at_utc
        age_text = (
            "—"
            if age is None
            else f"{(now - age).total_seconds() / 3600:.2f}h"
        )
        last = last_attack_at.get(target.coordinate)
        last_text = (
            "我方从未打过"
            if last is None
            else f"我方上次攻击距今 {(now - last).total_seconds() / 3600:.1f}h"
        )
        protected = protected_seen_at.get(target.coordinate)
        protected_text = (
            "未知"
            if protected is None
            else f"已知撞过保护期（{protected:%H:%M} UTC）"
        )
        sample_lines.append(
            f"    {target.coordinate} | 军力 {target.military_score:,.0f} | 龄 {age_text} | "
            f"往返 {trip_text} | {last_text} | 保护期 {protected_text}"
        )
    return "\n".join(summary_lines), "\n".join(sample_lines), len(samples), len(eligible)


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

    # -- 供调度器 --------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """开关 + 凭据 + 依赖三样齐了才叫可用。关着时 `observe` 零开销返回。"""
        if self._disabled_reason is not None:
            return False
        return bool(self._settings.ai_api_base and self._settings.ai_api_key)

    def observe(
        self,
        *,
        task_id: int,
        now: datetime,
        run_id: UUID | None,
        budget: int,
        eligible: Sequence[ScoredTarget],
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

        开关关掉、没有凭据、样本为空、线程已满、该任务还在冷却内，都返回 False
        且**不产生任何副作用**（需求第八节「开关关掉时零开销」）。
        """
        if not self.enabled:
            return False
        if budget < 1:
            return False
        if not eligible:
            return False
        # 开关已确认开着，才读三个行为旋钮（改动即生效）。这一步有库查询，
        # 所以必须排在 enabled / budget / eligible 三个零成本检查之后。
        sample_size, timeout_s, model = self._read_knobs()
        with self._lock:
            if self._active >= MAX_CONCURRENT_WORKERS:
                return False
            now_m = self._monotonic()
            if now_m - self._last_request_at.get(task_id, float("-inf")) < AI_SHADOW_MIN_INTERVAL_S:
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
                tuple(eligible),
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

    def _read_knobs(self) -> tuple[int, float, str]:
        """实时读三个行为旋钮（`ai_sample_size` / `ai_timeout_seconds` / `ai_model`）。

        读不到（表没初始化 / 配置行不存在）就回落 `__init__` 里的默认值——
        开关已经确认开着，回落只会让这一轮用默认参数，不会让调度受影响。
        """
        sample_size = self._sample_size
        timeout_s = self._timeout_s
        model = self._model
        try:
            row = self._repository.military_attack_config()
        except ValueError:
            return sample_size, timeout_s, model
        if row.ai_sample_size is not None:
            sample_size = int(row.ai_sample_size)
        if row.ai_timeout_seconds is not None:
            timeout_s = float(row.ai_timeout_seconds)
        if row.ai_model:
            model = row.ai_model
        return sample_size, timeout_s, model

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
        eligible: tuple[ScoredTarget, ...],
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
        sample_targets = [item.coordinate for item in eligible]
        last_attack = self._repository.last_bot_attack_at(sample_targets)
        protected = self._repository.bot_target_protection_seen_at(sample_targets)

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
            eligible=eligible,
            presets=presets,
            last_attack_at=last_attack,
            protected_seen_at=protected,
            sample_size=sample_size,
        )
        algorithm_picks_json = _algorithm_picks_json(assignments)
        reference = _soft_reference(eligible, origins, last_attack, protected, now)

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
                eligible,
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
        message = (
            f"AI 选靶影子：任务 {task_id} 记录为 {status}"
            + (
                f"（重合 {overlap}/{budget}，延迟 {latency_ms}ms，软核对 {len(violations)} 条）"
                if status == AiDecisionStatus.OK.value
                else f"（预算 {budget}）"
            )
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
        eligible: tuple[ScoredTarget, ...],
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
            targets=frozenset(item.coordinate for item in eligible),
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
    eligible: tuple[ScoredTarget, ...],
    origins: tuple[Coordinate, ...],
    last_attack_at: Mapping[Coordinate, datetime | None],
    protected_seen_at: Mapping[Coordinate, datetime | None],
    now: datetime,
) -> SoftReference:
    """软核对要的我方事实。`last_attack_at` / `protected_seen_at` 在线程里查好传入。"""
    military: dict[Coordinate, float] = {}
    ages: dict[Coordinate, float] = {}
    for item in eligible:
        if item.military_score is not None:
            military[item.coordinate] = item.military_score
        if item.military_score_at_utc is not None:
            ages[item.coordinate] = (now - item.military_score_at_utc).total_seconds() / 3600
    round_trip: dict[Coordinate, dict[Coordinate, float]] = {
        item.coordinate: {
            origin: round_trip_hours(item.coordinate, origin) * 60 for origin in origins
        }
        for item in eligible
    }
    protected_until: dict[Coordinate, datetime | None] = {
        coordinate: (
            None if seen_at is None else seen_at + timedelta(hours=GAME_PROTECTION_HOURS)
        )
        for coordinate, seen_at in protected_seen_at.items()
    }
    return SoftReference(
        military=military,
        reading_age_hours=ages,
        round_trip_minutes=round_trip,
        last_attack_at=last_attack_at,
        protected_until=protected_until,
        now=now,
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
    "build_prompt",
    "stratified_samples",
]
