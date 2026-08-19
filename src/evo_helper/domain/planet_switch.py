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

## 每一趟都要先回到顶部

⚠️ **关掉再打开，列表并不复位。** 实机 2026-08-19（生产 `system_log`）：
13:48:41 那一趟从第一屏 `['4:277:15', '9:250:88', '4:96:7']` 一路拖到
`['7:228:15', '1:55:6', '9:411:17']`；紧接着 13:49:11 与 13:49:40 两趟**第一屏
读到的就是那个底部**。而找那一行的方向只有一个（往下翻），于是排在顶部的
`4:277:15`、`9:250:8` 再也够不着——一屏就判 `list_exhausted`，直接 `NOT_FOUND`。
**这个缺陷会自我延续**：一次拖到底之后，后面每一趟都从底部开始，全部失败。

所以开列表之后先回顶（`game.planet_list.PlanetSwitcher._scroll_to_top`），
停止判据是 `reached_top`——**「拖了一下坐标还是那几个」，不是「拖够几次」**。

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

#: 派遣面板「起点」那一行里的坐标。那个 ROI 用的是纯数字白名单，中文与方括号都会
#: 被压成零星数字，所以这里只认三段数字、从噪声里挑出第一个来。
#:
#: ⚠️ **行星列表那一列不用这一条**，用下面那条带方括号的，理由见 `_PLANET_ROW_RE`。
_COORDINATE_RE = re.compile(r"(\d{1,3}):(\d{1,3}):(\d{1,3})")

#: 行星列表那一列的一行，**必须连方括号一起认出来**：`[2:137:18]`。
#:
#: ## 这一对方括号是「读多一位」的正因，实拍上量出来的
#:
#: 实机 2026-08-19：`9:250:8` 读成 `9:250:88`，于是 `find_row` 精确匹配不上，
#: 用户配的两颗出发星球一颗都切不过去。离线复现（`var/logs/` 三张行星列表实拍，
#: `dump-planet-list-unreadable-153847.png` 的第一屏与那天日志逐字相同）：
#:
#:     纯数字白名单  4x/LANCZOS  →  '14:277:15'   词框 (1129, 1189)
#:     带括号白名单  4x/LANCZOS  →  '[4:277:15]'  词框 (1129, 1189)
#:
#: **同一块像素、同一个词框**。词框从来都是罩着方括号的（三行一律 1130→1190），
#: 而白名单里没有 `[` `]`，Tesseract 只能给它们挑一个数字顶上——`[`→`1`/`5`、
#: `]`→`3`/`8`、连隔壁那行「行星大小」的 `/` 也被顶成 `7`（`158/200`→`1587200`）。
#: 顶出来的那一位再被宽松正则粘进相邻的一段，就是「多读一位」。
#:
#: 所以对策是**反过来**：把方括号放进白名单（`pirate_ui.PLANET_LIST_COORD_WHITELIST`），
#: 让 Tesseract 有地方安放它们，再要求一行必须是**成对括起来**的三段数字。
#: 方向由此变成「宁可读不出，不可读错」：括号一旦被顶成数字，这条正则就不匹配，
#: 那一行直接不成行——走的是「什么都不点」那条安全路径，而不是认到另一颗星球。
#:
#: 顺带挡掉了另一种实测错法：词被拦腰切开（`['[2:137:1', '5]']`）。老规则会把
#: `[2:137:1` 认成 `2:137:1`——**一颗真实存在的别的星球**。
#:
#: ⚠️ 括起来之后仍挡不住**括号内部**的替换（实拍上 3× LANCZOS 把 `[9:250:8]` 读成
#: `[8:250:8]`）。那一套本来就不在配方池里，凭据钉在
#: `tests/integration/vision/test_planet_switch_live.py`；这里只说清楚它没被这一条盖住。
_PLANET_ROW_RE = re.compile(r"\[(\d{1,3}):(\d{1,3}):(\d{1,3})\]")


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


def reads_as_a_planet_row(text: str) -> bool:
    """这个词框是不是一行**认得出来的**星球坐标（`[2:137:18]` 这个样子）？

    单独拆出来是为了让「哪一套配方算读出来了」（`tools.pirate_loop._planet_rows`）
    与「哪些词成得了行」（`rows_from_words`）共用同一个判据。两边各写一遍的话，
    迟早会出现「配方被采信了，可它给出的一行都不成行」这种自相矛盾的记录。
    """
    return _PLANET_ROW_RE.search(text) is not None


def rows_from_words(words: Iterable[tuple[int, str]]) -> tuple[PlanetRow, ...]:
    """把坐标列的 OCR 词框 `(中心 y, 文字)` 拧成一屏的行清单，按 y 从上到下排。

    **认不出坐标的词一律丢掉**，不留占位行：留下来的话调用方就有了一个「有行、
    但不知道是哪颗星球」的东西，而那正是会被按行号点出去的那种东西。

    同一屏上真实会混进来的噪声（实测于 `var/logs/calib-切换星球-基准.png`）：
    行星大小 `155/223` 读作 `155223` 或 `1587200`、图标排漏出来的零星 `5` / `75`。
    它们连一对方括号都凑不齐，`_PLANET_ROW_RE` 全挡得住。

    ⚠️ **必须成对括起来才算一行**，理由整段在 `_PLANET_ROW_RE`：那一对方括号既是
    「这一位是不是多出来的」的唯一凭据，也是「读不出」与「读成别的星球」之间
    唯一的那道闸。
    """
    rows: list[PlanetRow] = []
    for center_y, text in words:
        match = _PLANET_ROW_RE.search(text)
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


def reached_top(previous: Sequence[PlanetRow] | None, current: Sequence[PlanetRow]) -> bool:
    """往回拖了一下，列表还是原样 → 已经在顶部，别再往回拖了。

    比的是**坐标序列**，和 `list_exhausted` 同一个尺子——那正是信箱那条链路
    （`tools.pirate_loop._scroll_mail_list_to_top`）踩过的坑的反面：那边比的是
    邮件主题，而主题 OCR 在实拍上一字不差的是 0 行，于是「拖不动了」永远不成立，
    每一趟都白拖满 40 次上限（一次约 5.8 秒）。坐标比主题稳得多，但**也不是不会
    读错**（见 `_PLANET_ROW_RE`），所以读错的那一行现在压根不成行。

    两条各挡一种误判，都不许合并掉：

    - **`current` 一屏读空不算到顶。** 空的时候两屏的坐标序列都是 `[]`，直接比
      会当成「没动」而停手——而读空的意思是「这一帧没认出来」，多半是浮层盖着或
      OCR 失手，列表滚到哪根本无从谈起。信箱那条链路的注释里写死了同一条。
    - **头一次读（`previous is None`）不算到顶。** 那一屏是「拖之前」的样子，
      没有任何证据说明它就是顶部；当成到顶就等于「打开列表之后不回顶」，
      也就是这次要修的那个缺陷本身。
    """
    if not current or previous is None:
        return False
    return list_exhausted(previous, current)


def origin_in(raw_text: str) -> Coordinate | None:
    """从「起点」那一行的读数里挑出**第一个**三段数字当坐标；挑不出就 None。

    单独拆出来是为了让**判定**和**说清楚读到了什么**共用同一个解析器。
    `origin_confirmed` 只回答是非，而对不上时日志必须说出「读到的是哪一颗」——
    两边各写一遍解析，迟早会出现「判据说对不上、日志说对得上」这种自相矛盾的
    记录，而那正是 2026-08-17 那条「日志说假话比不说更糟」要防的东西。

    ⚠️ **None 的意思是「读不出」，不是「不是这一颗」。** 调用方必须把这两件事
    分开：读不出时该重读几帧（会动的画面上单帧的空结果是抛硬币，同
    `vision.scan_reading.read_panel_confirming`），重读仍读不出才按「核不过」收场；
    而绝不许当成「对上了」。
    """
    match = _COORDINATE_RE.search(raw_text or "")
    if match is None:
        return None
    galaxy, system, position = (int(part) for part in match.groups())
    return Coordinate(galaxy, system, position)


def origin_confirmed(raw_text: str, target: Coordinate) -> bool:
    """派遣面板「起点」那一行读回来的，是不是就是 `target`。

    这是「真的换过去了吗」的唯一判据。**读不出来算没切成**——返回 False，
    调用方本轮一发都不派。方向只能是这一个：漏判的代价是白等一轮，
    误判的代价是整轮的台账都在撒谎。

    宽在 ROI 会带进「起点：」的尾巴上（数字白名单会把中文压成零星数字），
    严在三段数字必须逐段相等：从读数里找出**第一个**形如 `d:d:d` 的三段数字
    再比。前缀噪声挑不出三段数字，所以只会被跳过，挑不出一个假的目标来。

    ⚠️ **切换那一刻确认过 ≠ 之后每一发都还站在那儿。** 实机 2026-08-18 18:53–18:56：
    切到 9:250:8 并回读确认，第一发确实从 9:250:8 飞出去（18.5 分，误差 0.5%），
    同一轮的第二发却是从主星 4:277:15 打出去的（125.0 分，误差 0%），而两发之间
    日志里**一条切星球记录都没有**——游戏自己退回了主星。所以「派出之前再核一次
    起点」是另一道闸门（`tools.pirate_loop.PirateLoop._require_origin_before_dispatch`），
    不是这一条的重复。
    """
    shown = origin_in(raw_text)
    return shown is not None and shown == target


__all__ = [
    "PlanetRow",
    "find_row",
    "list_exhausted",
    "origin_confirmed",
    "origin_in",
    "reached_top",
    "reads_as_a_planet_row",
    "rows_from_words",
    "switch_needed",
]
