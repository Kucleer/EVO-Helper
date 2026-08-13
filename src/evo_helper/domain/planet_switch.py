"""在游戏里切换「当前星球」这件事的**纯逻辑**：认哪一行、点哪一行、切没切成。

这里一个像素都不点、一张图都不拍。屏幕坐标住在 `game.pirate_ui`，动作住在
`game.planet_list`；分开是为了让下面这几条判据可以脱离游戏被钉住。

## 为什么必须先认坐标再点

行星列表浮层上，每颗星球一行、每行**八个图标，排成四列两排**（实拍
`var/logs/calib-切换星球-基准.png`）：

    上排 y≈名字行+60：运输 / 部署 / 传送 / 前往此处
    下排 y≈名字行+130：转移 / 投送 / 保护 / 扩张

八个里只有「前往此处」是我们要的，其余七个点错任何一个都是**真实操作**
（把资源送出去、把舰队部署出去、把星球让出去）。

⚠️ 两排这件事本身就是一个陷阱：「前往此处」正下方 70px 坐着「扩张」，
所以图标偏移量（`pirate_ui.PLANET_ICON_ROW_OFFSET_Y`）**改大一档不会报错**，
只会在实机上把星球扩张出去。`tests/unit/game/test_planet_list.py` 里单钉了这一条。

所以这里的规矩是：**认得出这一行的坐标才给出坐标点，认不出就一个点都不给**，
由调用方本轮什么都不点、退出等下一轮。绝不按行号盲点——行的顺序是游戏定的，
而「第二行就是 9:250:8」这种假设一旦不成立，代价是上面那七个图标之一。

## 位置只能来自当前这一屏

与 `game.preset_picker` 那条完全同形（那边是横向滚动的预设条，这边是纵向可拖的
行星列表）：拖一次 → **重读这一屏每一行的坐标** → 目标在不在这一屏？在就点
**当屏那一行**，不在就继续拖。

不缓存、不外推、**不跨屏换算 y**。列表的拖动步距从来没标定过，外推出来的 y
落到隔壁那一排图标上时，同一个 x（1166）坐着的是「扩张」。

「这一屏读到的和上一屏一样」= 拖到底了（`list_exhausted`）。仍没找到就什么都不点。

## 切没切成，只认回读

`origin_confirmed` 是**唯一**一份「真的换过去了吗」的判据。点完就当切成了是不行的：
那正好回到 #49 那道临时闸门要防的局面——舰队从主星飞出去，台账上写着别的坐标，
战报永远配不上。读不出来一律当作**没切成**。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from evo_helper.domain.models import Coordinate

#: 一行里的坐标形如 `[2:137:18]`，方括号在 OCR 白名单里被去掉了（`]` 会被读成 `3`，
#: 见 `vision.scan_reading.COORD_RECIPES`），所以这里只认三段数字。
_COORDINATE_RE = re.compile(r"(\d{1,3}):(\d{1,3}):(\d{1,3})")


@dataclass(frozen=True)
class PlanetRow:
    """行星列表上**这一屏**识别出来的一行。

    `name_row_y` 是「星球名 / 坐标」那一行的 y，也就是 OCR 词框自己的 y。
    它有两个用处，都必须跟着当前这一屏走：

    - 「前往此处」在它下面固定一段距离（`game.pirate_ui.PLANET_ICON_ROW_OFFSET_Y`）；
    - **纵向拖动的按下点也落在这一行**。用户口径（2026-08-13）：
      「星球名字高度，页面的横向中点，是可以进行拖动按下的点」。

    ⚠️ 按下点的 y **不能写死**。横向中点只在星球名那一行是空白，往下 60px 就是
    图标上排，同一个 x 上坐着「部署」。列表拖过之后行会移位，写死就会按在图标上，
    而**按下再拖起来游戏可能当成点击**（`tools.pirate_loop.slow_drag` 的注释里
    记着同一件事的反面）。
    """

    coordinate: Coordinate
    text: str
    name_row_y: int


def rows_from_words(words: Iterable[tuple[int, str]]) -> tuple[PlanetRow, ...]:
    """把坐标列的 OCR 词框 `(中心 y, 文字)` 拧成一屏的行清单，按 y 从上到下排。

    **认不出坐标的词一律丢掉**，不留占位行：留下来的话调用方就有了一个「有行、
    但不知道是哪颗星球」的东西，而那正是会被按行号点出去的那种东西。

    同一屏上真实会混进来的噪声（实测于 `var/logs/calib-切换星球-基准.png`）：
    行星大小 `155/223` 读作 `155223`、图标排漏出来的零星 `5` / `75`。
    三段数字这条规则把它们全挡在外面——它们连一个冒号都没有。
    """
    rows: list[PlanetRow] = []
    for center_y, text in words:
        match = _COORDINATE_RE.search(text)
        if match is None:
            continue
        galaxy, system, position = (int(part) for part in match.groups())
        rows.append(
            PlanetRow(
                coordinate=Coordinate(galaxy, system, position),
                text=text,
                name_row_y=center_y,
            )
        )
    return tuple(sorted(rows, key=lambda row: row.name_row_y))


def find_row(rows: Sequence[PlanetRow], target: Coordinate) -> PlanetRow | None:
    """这一屏里坐标**恰好等于** `target` 的那一行，没有就 None。

    相等而不是包含：`2:13:7` 是 `2:137:1` 的子串，按文字包含匹配会把两颗不同的
    星球当成一颗。行已经解析成 `Coordinate` 了，直接比三个整数。
    """
    for row in rows:
        if row.coordinate == target:
            return row
    return None


def switch_needed(target: Coordinate, current: Coordinate | None) -> bool:
    """这一轮还要不要切到 `target`。

    `current` 是 runner 记着的「本轮已经切到哪了」，**进程刚起来时一定是 None**：
    上一轮把游戏停在哪颗星球上是不可知的，所以开工那一次一定要切（哪怕配的就是
    主星——点自己那一行只是回到自己的地表，无害）。

    切过之后同一轮里再问就是 False，这就是「一轮只切一次」：切换属于开工阶段，
    不挂在每个目标前面。一个目标切一次的代价是每个目标多一次开浮层 + 一次拖动 +
    一次回读，而出发星球在一轮之内根本不会变。
    """
    return current != target


def list_exhausted(previous: Sequence[PlanetRow], current: Sequence[PlanetRow]) -> bool:
    """拖完这一下，列表还是原样 → 到底了，别再拖了。

    比的是**坐标序列**而不是 y：拖动带惯性，同一批行在两屏之间 y 会差几个像素，
    按 y 比会永远判「还能拖」，于是拖满上限次才罢休。
    """
    return [row.coordinate for row in previous] == [row.coordinate for row in current]


def origin_confirmed(raw_text: str, target: Coordinate) -> bool:
    """派遣面板「起点」那一行读回来的，是不是就是 `target`。

    这是「真的换过去了吗」的唯一判据。**读不出来算没切成**——返回 False，
    调用方本轮一发都不派。方向只能是这一个：漏判的代价是白等一轮，
    误判的代价是整轮的台账都在撒谎。

    宽在 ROI 会带进「起点：」的尾巴上（数字白名单会把中文压成零星数字），
    严在三段数字必须逐段相等：从读数里找出**第一个**形如 `d:d:d` 的三段数字
    再比。前缀噪声挑不出三段数字，所以只会被跳过，挑不出一个假的目标来。
    """
    match = _COORDINATE_RE.search(raw_text or "")
    if match is None:
        return False
    galaxy, system, position = (int(part) for part in match.groups())
    return Coordinate(galaxy, system, position) == target


__all__ = [
    "PlanetRow",
    "find_row",
    "list_exhausted",
    "origin_confirmed",
    "rows_from_words",
    "switch_needed",
]
