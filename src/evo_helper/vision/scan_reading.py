"""行星详情面板的 ROI 与读数核对规则。

这里是**唯一**一份「面板读出来的东西算不算数」的判据。扫描器和 `tools.ingest_scan`
都从这里取——校验规则各留一份的坑已经踩过一次（改了一处，另一处继续放行）。

面板有两种布局，判别方法是先试「有主」坐标框，读出合法坐标就按有主解析：

- **无主**（荒芜行星 / 敌对海盗）：行星名居中，坐标在其下。
- **有主**：坐标 / 玩家 / 联盟 三行靠左上。名字在 `owner`，不在 `planet_name`——
  只读后者会把所有 bot 当成空位漏掉。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace

#: 玩家名以此开头即判定为 bot（方案第 2 节）。
BOT_PREFIX = "bot_"

#: 系统占位行星，不是玩家，也不是可攻击目标。
UNOWNED_NAMES = {"荒芜行星", "未知", ""}

#: 面板 ROI，截图（client）空间，对应标定视口 1920×879 + 38px 标题栏。
OWNED_COORD_ROI = (830, 266, 1010, 292)
OWNED_PLAYER_ROI = (830, 288, 1080, 314)
FREE_NAME_ROI = (840, 352, 1080, 392)
FREE_COORD_ROI = (840, 394, 1080, 430)

#: 名字行放大 4×（实测配方，见交接文档第 4 节）。坐标行的配方见 COORD_RECIPES。
NAME_UPSCALE = 4

#: 坐标行按顺序试这几套配方，任一套核对通过就采信。
#:
#: 这个游戏的数字字体把 `1` 画成一根没有衬线的竖条，**相邻的 1 会粘成一根**：
#: `2:111:11` 读成 `2:10:11`、`2:112:6` 读成 `2:12:6`、位 11 读成位 1。
#: 受影响的不是零星几个坐标——位 11 是全宇宙的 1/16，`11x` 系是每百个恒星系里的十个。
#:
#: 两条对策，都是实测挑出来的（150 张样本，其中 63 张是实机失败样本）：
#:
#: - **放大用最近邻，不只用 LANCZOS**。LANCZOS 会把两根竖条之间那一两个像素的缝插值糊掉，
#:   最近邻把缝原样放大。两种各留一档，因为它们救回的样本并不重合。
#: - **白名单去掉方括号**。`]` 会被读成 `3`（`[2:91:9]` → `[2:91:39]`），而那个 9 位上
#:   住着 `bot_2_91_9`——丢的是 bot 本身。不让 tesseract 输出括号，这个错就没了。
#:
#: 单套最好的 x7-LANCZOS 覆盖 110/150，这四套的并集覆盖 135/150。
#: 顺序按单套强弱排，好让绝大多数坐标在第一套就通过。
COORD_RECIPES: tuple[tuple[int, str], ...] = (
    (7, "lanczos"),
    (7, "nearest"),
    (3, "nearest"),
    (3, "lanczos"),
)

#: 坐标行的字符白名单。**不含方括号**，理由见 `COORD_RECIPES`。
COORD_WHITELIST = "0123456789:"

#: 名字行的备用配方。只在名字**看着像被读坏的 bot 名**时才逐套重读。
#:
#: 实机上 `bot_2_9_5` 读成 `botleao.-`——连 `bot_` 前缀本身都糊掉了。
#: bot 判定就是看这个前缀，所以那颗星球被当成普通空位入了库，
#: **而它正是我们要找的东西**。这类失败不会报错，靠「每系恰好一个 bot」的分布才发现。
NAME_RECIPES: tuple[tuple[int, str], ...] = (
    (4, "lanczos"),
    (2, "lanczos"),
    (6, "lanczos"),
    (4, "nearest"),
)

#: 「像 bot 名但前缀不对」的判据：以 bot 开头、但下一个字符不是下划线。
_MANGLED_BOT_RE = re.compile(r"^bot(?!_)", re.IGNORECASE)


def looks_like_mangled_bot(name: str | None) -> bool:
    """名字疑似 bot 但前缀读坏了。

    只用来决定**要不要换配方重读**，绝不据此直接判定为 bot——
    真有玩家叫 `botanist` 的话，放宽前缀就会把人当成 bot。
    """
    return bool(name) and bool(_MANGLED_BOT_RE.match(str(name)))


COORDINATE_RE = re.compile(r"(\d{1,3}):(\d{1,3}):(\d{1,3})")

#: bot 名形如 ``bot_<银河>_<恒星>_<行星>``，`bot_` 之后只有数字和下划线。
#: OCR 在这种小字号上会把 1 读成 l、2 读成 e、0 读成 O。
_BOT_DIGIT_FIX = {"l": "1", "I": "1", "e": "2", "O": "0", "o": "0", "S": "5", "B": "8"}

#: 读一行文字：``(box, digits=..., upscale=...) -> str``。
#: 数字行走白名单 + eng，文字行走 chi_sim+eng——混合语言下开头的 `2` 会被读成 `e`。
OcrLine = Callable[..., str]


def digits_of(text: str) -> str:
    """取出文本里的数字序列。

    坐标里的冒号又细又矮，OCR 会漏读（``[2:122:9]`` 读成 ``[2122:9]``）。
    但我们不是在自由解析坐标——我们在核对面板显示的是否**就是请求的那个**坐标。
    数字序列相等即可证明这一点，且对漏读分隔符免疫。
    """
    return "".join(ch for ch in text if ch.isdigit())


def bracketed(text: str) -> str:
    """取方括号里的部分；没有方括号就原样返回。

    ROI 会带进「坐标：」的尾巴，数字白名单把它读成拉丁噪声——实测出现过
    ``4:[2:6:15]``，那个多出来的 ``4`` 会让核对失败。面板上的坐标一定在方括号里，
    以左括号为界就能把前缀噪声挡掉。

    **只挡前缀，不放松判据**：括号里的数字序列仍要与请求逐位相等。
    """
    start = text.rfind("[")
    if start < 0:
        return text
    end = text.find("]", start)
    return text[start + 1 : end if end > start else len(text)]


def coordinate_confirmed(requested: str, raw_text: str) -> bool:
    """面板读回来的是不是**就是请求的那个**坐标。

    两条互补的判据，任一成立即通过——两条都是单向的，只会漏判不会错判：

    1. **请求串原样出现在读数里。** 允许两侧有噪声：方括号会被读成数字
       （`[2:12:9]` 读成 `[2:12:93]`，那个 `3` 是右括号），ROI 也会带进「坐标：」的尾巴。
       结构完整时这条最严——银河系是一位数、分隔符位置固定，
       别的合法坐标的文本里凑不出请求串。
    2. **数字序列相等。** 冒号又细又矮，会被整个漏读（`[2:122:9]` → `[2122:9]`），
       这时结构没了，只能比数字。这条比第 1 条松，所以放在后面兜底。
    """
    if not raw_text:
        return False
    if requested in raw_text:
        return True
    return digits_of(bracketed(raw_text)) == digits_of(requested)


def is_bot_name(name: str | None) -> bool:
    return bool(name) and str(name).startswith(BOT_PREFIX)


def normalise_bot_name(name: str) -> str:
    """把 bot 名后半段里被误读成字母的数字还原。

    只在 ``bot_`` 前缀之后动手，且只替换已知的混淆字符——前缀本身不碰，
    因为 bot 判定就靠它，改前缀等于改判定结果。
    """
    if not is_bot_name(name):
        return name
    head, tail = name[: len(BOT_PREFIX)], name[len(BOT_PREFIX) :]
    return head + "".join(_BOT_DIGIT_FIX.get(ch, ch) for ch in tail)


def owner_of(name: str | None) -> str | None:
    """占位行星没有归属；返回 None 而不是把「荒芜行星」当成玩家名。"""
    if name is None or name.strip() in UNOWNED_NAMES:
        return None
    return name.strip()


@dataclass(frozen=True)
class PlanetPanel:
    """一次面板读数。``layout`` 记下走了哪条解析分支，便于事后复盘。"""

    layout: str
    coordinate_text: str
    planet_name: str | None = None
    owner: str | None = None

    @property
    def display_name(self) -> str | None:
        """归属名：有主布局在 ``owner``，无主布局在 ``planet_name``。"""
        name = owner_of(self.owner or self.planet_name)
        return normalise_bot_name(name) if name is not None else None

    @property
    def is_bot(self) -> bool:
        return is_bot_name(self.display_name)

    def confirms(self, requested: str) -> bool:
        return coordinate_confirmed(requested, self.coordinate_text)


def read_panel(
    ocr_line: OcrLine, *, coord_recipe: tuple[int, str] = COORD_RECIPES[0]
) -> PlanetPanel:
    """按两种布局读面板。先试有主，读出合法坐标就按有主解析。"""
    upscale, resample = coord_recipe
    owned = ocr_line(OWNED_COORD_ROI, digits=True, upscale=upscale, resample=resample)
    if COORDINATE_RE.search(owned):
        return PlanetPanel(
            layout="owned",
            coordinate_text=owned,
            owner=ocr_line(OWNED_PLAYER_ROI, digits=False, upscale=NAME_UPSCALE) or None,
        )
    return PlanetPanel(
        layout="free",
        coordinate_text=ocr_line(FREE_COORD_ROI, digits=True, upscale=upscale, resample=resample),
        planet_name=ocr_line(FREE_NAME_ROI, digits=False, upscale=NAME_UPSCALE) or None,
    )


def reread_owner(ocr_line: OcrLine, first_read: str | None) -> str | None:
    """名字疑似被读坏时换配方重读；读出正经的 `bot_` 前缀才采纳。

    只往「更像 bot」的方向纠正，且必须是**真的读出来**的，不是拼出来的——
    放宽前缀判定会把名叫 `botanist` 的真人当成 bot。
    """
    if not looks_like_mangled_bot(first_read):
        return first_read
    for upscale, resample in NAME_RECIPES:
        candidate = ocr_line(
            OWNED_PLAYER_ROI, digits=False, upscale=upscale, resample=resample
        ).strip()
        if is_bot_name(candidate):
            return candidate
    return first_read


def read_panel_confirming(ocr_line: OcrLine, requested: str) -> PlanetPanel:
    """逐套配方读，直到坐标核对通过；都不过就返回最后一次读数。

    重点是**在同一张截图上换配方**，而不是重新导航。粘连是读不出，不是没跳过去——
    实测重新导航读三遍，三遍都错在同一处。绝大多数坐标第一套就通过，后面几套只在
    出问题时才跑，所以稳态耗时不受影响。
    """
    panel = read_panel(ocr_line)
    for recipe in COORD_RECIPES:
        panel = read_panel(ocr_line, coord_recipe=recipe)
        if panel.confirms(requested):
            break
    if panel.layout != "owned" or not looks_like_mangled_bot(panel.owner):
        return panel
    # 名字看着像被读坏的 bot 名——换配方重读一次，否则这颗星球会被当成空位入库。
    return replace(panel, owner=reread_owner(ocr_line, panel.owner))
