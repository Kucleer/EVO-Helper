"""按 bot 舰队规模分档，决定用哪套攻击组合。

用户的实际需求不是精确数量，是**落在哪一档**（默认三道边界 2K / 4K / 8K）：

    2K 以下 → 不派
    2K–4K   → 预设 AAA
    4K–8K   → 预设 BBB
    8K+     → 预设 CCC

所以识别的目标随之改变：`5.36K` 读成 `5.35K` 无所谓，读成 `.36K` 才致命——
差一个数量级就会换错组合。防的是**量级错**，不是末位误差。

游戏里大数显示成 `5.36K` 这样的四舍五入值，所以精确总数本来就取不到；
逐行相加更凑不出精确值。分档口径正好绕开了这件事。

## 三道边界可配，但**这个模块不去查它**

阈值由用户在控制台的「分档阈值」页上改，存在 `scheduler_config`。这里只有
`TierThresholds` 这个值对象和几个纯函数：取值从调用方传进来。

分档这件事本身是**现算**的——库里没有任何一列存过档位结论，`tier_for` 的输出
只活在一次派遣决策里。所以改阈值只影响改完之后发出的攻击，历史一行都不动
（想知道过去某一发当时算成了哪一档，看 `attack_dispatches.preset_name`：
那是那一发实际用掉的预设标题，不要拿今天的阈值去重算 `defender_units`）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

#: 数量文本：`517`、`5.36K`、`1.09K`。K 是千。
_COUNT_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([Kk])?$")


class FleetTier(Enum):
    """舰队规模档位。

    值刻意**不含数字**：三道边界可配之后，写死 `"2K–5K"` 的标签迟早和实际用的
    阈值对不上，而对不上的那一刻页面和日志上都看不出来。要给人看的区间文字问
    `TierThresholds.label()`，它按当前取值现算。
    """

    #: 小到不值得为它挑组合——用户明确说过最低那一档的误差可以完全忽略。
    NEGLIGIBLE = "不打"
    ALPHA = "低档"
    BETA = "中档"
    GAMMA = "高档"

    @property
    def preset(self) -> str | None:
        """这一档该用的攻击组合；`NEGLIGIBLE` 档不派。"""
        return _TIER_PRESETS.get(self)


#: 各档用的**游戏内预设标题**。用户确认（2026-08-09）：AAA / BBB / CCC。
#:
#: 这里存的必须是**标题原文**，因为派遣链路是按标题在预设条上 OCR 找的
#: （`game.preset_picker`）：标题对不上就抛 `PresetNotFound`，整发放弃。
#: 这不是假想的风险——实机日志有过 `预设条上找不到 'CCC'；这一屏读到的是
#: ['AAA', '探路']`，成因是选择器只往左拖、够不到右边的预设（PR #100 已修）。
#: **改这里之前对着游戏的预设条核一遍标题。**
#: 预设里装了什么由用户在游戏里维护，助手不读也不校验。
_TIER_PRESETS = {
    FleetTier.ALPHA: "AAA",
    FleetTier.BETA: "BBB",
    FleetTier.GAMMA: "CCC",
}

#: 离边界这么近就标出来。分档只在边界附近才怕读错——
#: `5.36K` 读成 `5.35K` 不影响任何判断，`4.98K` 读成 `5.02K` 却会换一套组合。
BOUNDARY_MARGIN = 200


class TierThresholdError(ValueError):
    """三道边界不成立。**拒绝，不排序也不截断。**

    悄悄把用户填的数排好序收下，页面上就会显示成「保存成功」，而实际生效的是
    另外三个数；截断同理。两种都属于「页面说的和调度器做的不是一回事」。
    """


@dataclass(frozen=True)
class TierThresholds:
    """三道档位边界（单位：艘）。每一个都是那一档的**下界，闭区间**。

    左闭右开是原本就有的语义（`total == alpha_from` 属于 ALPHA），这里只是把
    三个数从模块常量变成了一个可以传递的值。命名按「哪一档从这里开始」而不是
    「哪一档到这里为止」：后者要在文档里额外交代一句「不含」，而每一处漏读那句
    话都是一次边界差一档。
    """

    #: 低于它一律不派（默认 2000）。
    alpha_from: int
    #: 到了它就换 BBB（默认 4000）。
    beta_from: int
    #: 到了它就换 CCC（默认 8000）。
    gamma_from: int

    def __post_init__(self) -> None:
        self._require_positive()
        self._require_increasing()

    @property
    def edges(self) -> tuple[int, int, int]:
        """三道边界，由小到大。`classify` 的「离边界多近」按它算。"""
        return (self.alpha_from, self.beta_from, self.gamma_from)

    def label(self, tier: FleetTier) -> str:
        """这一档在界面和日志里念作什么，例如 `2K–4K`。

        按当前取值现算，所以改了阈值之后日志里那句话跟着改。写死在
        `FleetTier` 上的话，改完阈值日志还会照旧念旧区间。
        """
        if tier is FleetTier.NEGLIGIBLE:
            return f"{_kilo(self.alpha_from)} 以下"
        if tier is FleetTier.ALPHA:
            return f"{_kilo(self.alpha_from)}–{_kilo(self.beta_from)}"
        if tier is FleetTier.BETA:
            return f"{_kilo(self.beta_from)}–{_kilo(self.gamma_from)}"
        return f"{_kilo(self.gamma_from)}+"

    def _require_positive(self) -> None:
        for name, value in zip(_EDGE_NAMES, self.edges, strict=True):
            if value < 1:
                raise TierThresholdError(f"{name}要大于 0（收到 {value}）")

    def _require_increasing(self) -> None:
        """必须严格递增，否则中间那一档成了取不到的死区。

        举例：把 BBB 的起点设成 9000 而 CCC 仍是 8000，那么 8000 以上一律先撞上
        CCC，BBB 再也轮不到——而页面上三个框都填着数，看起来一切正常。相等同样
        要拒：`beta_from == gamma_from` 时 BBB 的区间宽度为零。
        """
        pairs = list(zip(_EDGE_NAMES, self.edges, strict=True))
        for (low_name, low), (high_name, high) in zip(pairs, pairs[1:], strict=False):
            if low >= high:
                raise TierThresholdError(
                    f"分档阈值必须严格递增：{low_name} {low} 不小于 {high_name} {high}，"
                    f"这会让中间那一档永远取不到（页面上看不出来）"
                )


#: 三个数在错误信息里怎么念。位置与 `TierThresholds.edges` 一一对应。
_EDGE_NAMES = ("AAA 起点", "BBB 起点", "CCC 起点")

#: 用户口径（2026-08-11）：2K 以下不打、2K–4K 打 AAA、4K–8K 打 BBB、8K+ 打 CCC。
#:
#: 它只是**新库的初值**（`storage.models.SchedulerConfigRow` 的列默认值抄的就是
#: 这三个数）。真正生效的取值一律从 `scheduler_config` 读——在这里回落到默认，
#: 就等于让某条路径悄悄用一套和页面上显示的不一样的数。
DEFAULT_TIER_THRESHOLDS = TierThresholds(alpha_from=2000, beta_from=4000, gamma_from=8000)


def parse_fleet_count(text: str) -> int | None:
    """把 `5.36K` / `517` 解析成艘数；认不出返回 None。

    `K` 是游戏自己的四舍五入显示，`5.36K` 的真实值在 5355–5364 之间。
    这里取 5360——档位判断用不着更准。

    ⚠️ **`M` 是故意不认的，不是漏了。** 读到 `1.5M` 返回 None，调用方那边
    「没读到」的处置是**不打**（`tools.bot_loop._tier_and_attack`），
    而认了它就等于凭一个从未在实机上见过的后缀把舰队送出去。
    识别侧的白名单本来也只放行 `0123456789.K`
    （`vision.optional.report_screens.UNIT_WHITELIST`），`M` 根本进不来；
    真有一天游戏开始显示 `M`，要改的是那条白名单和这里，两处一起改一次。

    ⚠️ 这个函数**不是** 2026-08-11 那次量级错的成因。2:48:12 的守方单位
    实为 `1.22K`，`parse_fleet_count("1.22K")` 给出 1220（正确）；入库的
    122000 来自 `parse_fleet_count("122K")`——小数点在 OCR 那一层就掉了。
    修在选票那一层（`vision.fleet_counts.pick_count`），不在这里。
    """
    match = _COUNT_RE.match(text.strip())
    if match is None:
        return None
    value = float(match.group(1))
    return round(value * 1000) if match.group(2) else round(value)


def tier_for(total: int, thresholds: TierThresholds) -> FleetTier:
    """总数落在哪一档。边界取左闭右开：`total == beta_from` 属于 BBB 那一档。

    阈值**必须传**，没有默认值：给它一个默认，任何一处忘了传的调用方都会静默
    地按另一套数分档，而分档错的后果是派错一整套舰队。
    """
    if total < thresholds.alpha_from:
        return FleetTier.NEGLIGIBLE
    if total < thresholds.beta_from:
        return FleetTier.ALPHA
    if total < thresholds.gamma_from:
        return FleetTier.BETA
    return FleetTier.GAMMA


@dataclass(frozen=True)
class TierVerdict:
    total: int
    tier: FleetTier
    near_boundary: bool

    @property
    def preset(self) -> str | None:
        return self.tier.preset


def classify(
    total: int, thresholds: TierThresholds, *, margin: int = BOUNDARY_MARGIN
) -> TierVerdict:
    """定档，并标出「离边界太近、读数误差可能改变结论」的情形。

    这是识别误差唯一真正要紧的地方。档位中间的读数错几十艘没有后果；
    边界附近错几十艘就会换一套攻击组合。
    """
    return TierVerdict(
        total=total,
        tier=tier_for(total, thresholds),
        near_boundary=any(abs(total - edge) <= margin for edge in thresholds.edges),
    )


def _kilo(value: int) -> str:
    """`2000` → `2K`，`4500` → `4.5K`，`900` → `900`。

    只为显示。整千不拖 `.0`，因为界面上那几档默认全是整千数，多两个字符会让
    「2K–4K」这种一眼能读的区间变成需要看的。
    """
    if value < 1000:
        return str(value)
    return f"{f'{value / 1000:.2f}'.rstrip('0').rstrip('.')}K"


__all__ = [
    "BOUNDARY_MARGIN",
    "DEFAULT_TIER_THRESHOLDS",
    "FleetTier",
    "TierThresholdError",
    "TierThresholds",
    "TierVerdict",
    "classify",
    "parse_fleet_count",
    "tier_for",
]
