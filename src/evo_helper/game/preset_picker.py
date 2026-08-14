"""在派遣面板里选一个游戏内预设。

**为什么必须显式选预设**：派遣面板会保留上一次的选择。实机上打开时那里躺着
「轻型战斗机 1000」——直接点绿✓再出发，就把一千架轻型战斗机送去打海盗了。
所以攻击链路必须「选预设 → 回读校验」，两步都不能省。

预设条是**连续横向滚动**的，一屏只看得见约两个预设，而且**打开时的滚动位置不固定**：
实机上 AAA 在最左端，面板却是从「探路 / BBB」那一段打开的。所以流程是
「先拖到左端夹住 → 再一屏一屏往右找」，不能假设第一屏就有想要的那个。

⚠️ **只在左端那一屏找是不够的。** 曾经如此，代价是：左端那一屏是 `AAA / 探路`，
BBB 和 CCC 在更右边，于是它俩**永远进不了候选**，报出来的是「预设条上找不到
'CCC'」，看上去像游戏里没有这个预设。它有，只是没拖到。

这条从「两档派不出去」升级成了**整条 bot 链路派不出去**：bot 现在每一发用的
都是 BBB（`domain.bot_round.BOT_ATTACK_PRESET`），而 BBB 正是要往右拖才看得到
的那一档。

## 往右拖，同时保证点不到「+ 保存当前舰队」

⚠️ **预设条最右端是「+ 保存当前舰队」**，点到它会覆盖用户的预设——这是整条链路上
唯一会改坏用户配置的控件。原先的做法是「一步也不往右拖」，简单但把右边的预设一并
关在了门外。现在往右拖，安全由三条各自独立的闸挡着：

1. **按下的手指不进边距**：往右拖的起点 `PRESET_DRAG_RIGHT_FROM_X` 在
   `PRESET_SAFE_CLICK_MAX_X` 左边。（左拖是按在 800、松手到 1150；松手落在按钮上
   不触发它，所以左拖坐标原样不动。）
2. **点不进边距**：命中的中心 x 落在 `PRESET_SAFE_CLICK_MAX_X` 右边一律不点，
   当作这一屏没找到、继续拖。往右拖会把它带进安全区，下一屏再点。
3. **按名字也不点**：读出来含 `PRESET_SAVE_BUTTON_KEYWORD` 的一律不当候选。

## 位置只能来自当前这一屏

用户口径（2026-08-11）：「这里你需要识别文本进行定位，而不是直接定位」。

所以点击用的 x **必须是这一次在当前截图上 OCR 出来的中心 x**：拖一次 → 重读这一屏
→ 目标在不在这一屏读出来的名字里？在就点它当屏的位置，不在就继续拖。
不缓存、不累加、不把别的屏上的 x 换算过来——预设条是连续滚动的，拖动步距从来没标定
过、还带惯性，外推出来的坐标站不住，而点偏的代价是选错预设、送错舰队。

顺带的好处：合并（见 `merged_names`）永远只在**一屏之内**发生，跨屏两个词被 x
凑到一起误合成一个名字这件事根本不会出现。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from evo_helper.game.pirate_ui import (
    PRESET_DRAG_FROM_X,
    PRESET_DRAG_RIGHT_FROM_X,
    PRESET_DRAG_RIGHT_TO_X,
    PRESET_DRAG_TO_X,
    PRESET_DRAG_Y,
    PRESET_MAX_DRAGS,
    PRESET_NAME_ROW_Y,
    PRESET_SAFE_CLICK_MAX_X,
    PRESET_SAVE_BUTTON_KEYWORD,
    PRESET_TOGGLE,
)

#: 预设名那一行的 ROI 与 OCR 配方（917 空间，实机量于 2026-08-09）。
#: 右界收到 1000：再往右是第二个预设的数量列，读进来只是噪声。
#: 3× + `chi_sim+eng` + psm 6 实测能同时读出 `AAA` 与 `探路`；
#: 只跑 `eng` 会把中文预设名读成 `PRIS` 之类，于是中文名的预设永远选不中。
PRESET_NAME_ROI = (730, 684, 1000, 704)
PRESET_NAME_UPSCALE = 3

#: 金色掩膜那一档用的**宽** ROI：整条预设条都收进来。
#:
#: 上面那条把右界收到 1000，理由是「再往右是第二个预设的数量列，读进来只是噪声」。
#: **那条理由在掩膜下不成立**：数量列是白字，按金色抠完根本不在图里。
#:
#: 而宽一点是必须的：实机 2026-08-15 量到，窄 ROI 里 `BBB` 的像素明明在
#: （148 个黑点），tesseract 却读成空串——一张大片空白里只有一个孤零零的词时
#: 它认不出来。把 `CCC` 一起收进来（238 个黑点）就一次读对。
PRESET_NAME_ROI_WIDE = (730, 682, 1240, 706)

#: 展开预设条后等它铺开。
PRESET_OPEN_WAIT_S = 1.6

#: 一次拖动之后等惯性停下。
PRESET_DRAG_WAIT_S = 1.2

#: 相邻两个词框中心相距不超过这么多像素，就当成**同一个预设名被 OCR 拆开了**。
#:
#: tesseract 的分词对中文是按字切的：`AAA` 读回来是一个词，`探路` 读回来是
#: `探` 和 `路` 两个。逐词做 `name in text` 于是永远匹配不上中文预设名——
#: 实机后果是 bot 链路每一发都倒在「找不到预设 探路」，而预设条上明明有它。
#:
#: 阈值取 40 的依据（2026-08-11 实测，预设条拖到左端后的词框中心）：
#:
#:     AAA  x=747
#:     探   x=984
#:     路   x=994      ← 同名相邻两字差 10px
#:                       不同预设之间差 237px
#:
#: 两个量级中间隔着一个数量级，40 离两边都远。这个余量比精确值重要：
#: 拆得更碎（比如三字名）时字距仍是十几像素，而预设之间永远隔着大半个格子。
PRESET_WORD_GAP_PX = 40

#: 一屏读到空清单时，最多重读几次、每次之间等多久。
#:
#: 预设条**不可能真的是空的**（理由见 `PresetPicker.read_names_confirming`），
#: 所以空结果只能是这一帧没读出来。3 次 × 0.6 秒 ≈ 多花 1.2 秒，而它挡住的是
#: 「整发放弃」——实机 2026-08-13 那一夜为这件事白跑了约两小时。
PRESET_READ_ATTEMPTS = 3
PRESET_REREAD_WAIT_S = 0.6


class PresetDriver(Protocol):
    def click(self, x: int, y: int, *, label: str = ...) -> None: ...

    def drag(self, from_x: int, from_y: int, to_x: int, to_y: int, *, label: str = ...) -> None: ...

    def wait(self, seconds: float) -> None: ...


class PresetNotFound(RuntimeError):
    """从左端一路拖到右端，每一屏都没读到这个名字。

    **不许退而求其次点一个别的**：预设决定送出去多少舰队，选错的代价是真实的舰队。
    """


@dataclass
class PresetPicker:
    """按名字选预设。`read_names` 交出这一屏预设名的 `(中心 x, 文字)`。"""

    driver: PresetDriver
    read_names: Callable[[], Sequence[tuple[int, str]]]

    def read_names_confirming(self) -> Sequence[tuple[int, str]]:
        """读这一屏的预设名，**空结果要重读几次再认**。

        ⚠️ **预设条不可能真的是空的。** 它至少有一个预设在视野里——用户的舰队
        预设是他自己在游戏里维护的，一个都没有的话这整条链路根本无从谈起。
        所以「这一屏读到 0 个名字」只可能是**这一帧没读出来**（拖动动画还没停、
        或者 OCR 失手），绝不是「这一段没有预设」。

        实机 2026-08-13 通宵，这一条把整夜毁了：预设顺序是 AAA / 探路 / BBB / CCC
        （用户 2026-08-14 确认），而 `pick('BBB')` 逐屏读到的是

            [['AAA', '探路'], [], ['ccc'], ['ccc']]

        第 2 屏正是 BBB 那一屏，读成了空、被当成「这儿没有」，于是往右拖过头，
        再看到两屏相同的 ccc 就判「到右端了」，抛 `PresetNotFound`。
        **这样白跑了 145 次，每次约 50 秒，合计约 2 小时**——「18 分钟才派出
        第一发」「四轮一发没派」全是它。

        与 `vision.scan_reading.read_panel_confirming`、`game.session_keeper.observe`
        同一条规矩：会动的画面上，单帧的空结果是抛硬币，不是证据。
        """
        for _attempt in range(PRESET_READ_ATTEMPTS):
            names = list(self.read_names())
            if names:
                return names
            self.driver.wait(PRESET_REREAD_WAIT_S)
        return []

    def expand(self) -> None:
        self.driver.click(*PRESET_TOGGLE, label="预设条")
        self.driver.wait(PRESET_OPEN_WAIT_S)

    def scroll_to_left_end(self, *, max_drags: int = PRESET_MAX_DRAGS) -> Sequence[tuple[int, str]]:
        """一直往左拖到夹住，返回夹住之后这一屏的预设名。

        判据是「这一屏读到的名字不再变化」，不是拖固定次数：拖多少次能到左端
        取决于打开时停在哪。实测从「探路 / BBB」那一段出发，两次就夹住了。

        两处用它：找之前定起点，以及**放弃之前把条还原成左端**（见 `pick`）。
        """
        seen = list(self.read_names_confirming())
        for _attempt in range(max_drags):
            self.driver.drag(
                PRESET_DRAG_TO_X,
                PRESET_DRAG_Y,
                PRESET_DRAG_FROM_X,
                PRESET_DRAG_Y,
                label="预设条左移",
            )
            self.driver.wait(PRESET_DRAG_WAIT_S)
            current = list(self.read_names_confirming())
            if _names_of(current) == _names_of(seen):
                return current
            seen = current
        return seen

    def scroll_right_once(self) -> Sequence[tuple[int, str]]:
        """往右拖一格（内容左移、露出右侧），返回**拖完之后**这一屏的预设名。

        起点在 `PRESET_SAFE_CLICK_MAX_X` 左边：按下的手指绝不落进
        「+ 保存当前舰队」那条边距里。
        """
        self.driver.drag(
            PRESET_DRAG_RIGHT_FROM_X,
            PRESET_DRAG_Y,
            PRESET_DRAG_RIGHT_TO_X,
            PRESET_DRAG_Y,
            label="预设条右移",
        )
        self.driver.wait(PRESET_DRAG_WAIT_S)
        return list(self.read_names_confirming())

    def pick(self, name: str) -> int:
        """展开、拖到左端、再一屏一屏往右找，点中名叫 `name` 的那个预设，返回其中心 x。

        返回的 x 就是**点下去的那个 x**，且它一定来自命中那一屏刚读出来的词框——
        任何一屏都只用它自己的 OCR 结果定位，见模块头「位置只能来自当前这一屏」。

        找不到就抛 `PresetNotFound`——由调用方决定放弃这一发，而不是凑合点一个。
        """
        self.expand()
        entries = list(self.scroll_to_left_end())
        screens: list[list[str]] = []
        previous: list[str] | None = None
        for _attempt in range(PRESET_MAX_DRAGS + 1):
            runs = merged_names(entries)
            screens.append([text for _x, text in runs])
            target = _clickable_hit(runs, name)
            if target is not None:
                self.driver.click(target, PRESET_NAME_ROW_Y, label=f"预设 {name}")
                self.driver.wait(PRESET_DRAG_WAIT_S)
                return target
            words = _names_of(entries)
            if previous is not None and words == previous:
                break  # 右端也夹住了，右边没有更多预设了。
            previous = words
            entries = list(self.scroll_right_once())
        # 一个都没点，但条被拖到了右端——那正是「+ 保存当前舰队」露脸的位置。
        # 下游坐标（比如 `DISPATCH_CONFIRM` (1156, 763)，落在 `PRESET_STRIP_ROI` 里）
        # 都是在条停在左端时标定的，所以交还控制权之前先拖回左端，还原成标定时的样子。
        #
        # ⚠️ **点中之后不这么做**，那是刻意的不对称：选中预设之后条是什么状态未知
        # （实机上紧接着点 `DISPATCH_CONFIRM` 就能成，说明条已经不挡着那一点了），
        # 这时再在 `PRESET_DRAG_Y=760` 上拖一把，划过的是派遣面板的「恒星系」那一行
        # （`DESTINATION_SYSTEM_ROI` y=746–776）——一次没人验过的操作，
        # 换来的只是「让状态更整齐」。不划算。
        self.scroll_to_left_end()
        raise PresetNotFound(f"预设条上找不到 {name!r}；从左到右逐屏读到的是 {screens}")


def gold_mask(crop: Any) -> Any:
    """把金黄的预设名抠成**黑字白底**，背景一律刷白。

    ⚠️ **灰度化在这一行上会瞎掉。** 实机 2026-08-15：预设条第 4 页上的 `BBB`
    灰度读出来是空串（3×/4×/6×/8× 全空），而金色掩膜 3× 一次就读对。
    原因是金字压在蓝底上，两者**亮度接近**——金 (255,200,0) 灰度约 193，
    背景蓝约 79–150，滚到亮一点的那一段就没有对比度了。掩膜按 `r - b` 判，
    与背景明暗无关。

    这条判据 2026-08-15 那一夜代价很大：bot 链路整晚找不到 `BBB`，
    一发都没派，而 `BBB` 就明明白白印在屏幕上。
    """
    from PIL import Image

    rgb = crop.convert("RGB")
    width, height = rgb.size
    source = list(rgb.getdata())
    painted = [
        0 if (red > 140 and green > 110 and red - blue > 60) else 255 for red, green, blue in source
    ]
    mask = Image.new("L", (width, height))
    mask.putdata(painted)
    return mask


def name_words(image: Any, ocr: Any) -> list[tuple[int, str]]:
    """从一张整窗截图里读出预设名那一行的 `(中心 x, 文字)`。

    用词框而不是整行文本：要拿 x 去点。

    ⚠️ **两档配方，读到就算。** 灰度那一档在预设条**某些滚动位置**上完全读不出来
    （见 `gold_mask`），而它在别的位置上一直好用、也是中文预设名验过的那一档。
    所以不是替换是加法：灰度先试，读空了再用金色掩膜兜。
    """
    recipes = (
        (PRESET_NAME_ROI, image.crop(PRESET_NAME_ROI).convert("L")),
        (PRESET_NAME_ROI_WIDE, gold_mask(image.crop(PRESET_NAME_ROI_WIDE))),
    )
    for roi, prepared in recipes:
        scaled = prepared.resize(
            (prepared.width * PRESET_NAME_UPSCALE, prepared.height * PRESET_NAME_UPSCALE),
            _lanczos(image),
        )
        data = ocr.image_to_data(
            scaled, lang="chi_sim+eng", config="--psm 6", output_type=ocr.Output.DICT
        )
        words: list[tuple[int, str]] = []
        for index, word in enumerate(data["text"]):
            text = word.strip()
            if not text:
                continue
            left = roi[0] + data["left"][index] // PRESET_NAME_UPSCALE
            width = data["width"][index] // PRESET_NAME_UPSCALE
            words.append((left + width // 2, text))
        if words:
            return words
    return []


def merged_names(entries: Sequence[tuple[int, str]]) -> list[tuple[int, str]]:
    """把靠得足够近的相邻词框合成一个预设名，返回 `(中心 x, 完整名字)`。

    见 `PRESET_WORD_GAP_PX`：中文名会被 tesseract 按字切开，不合并就永远匹配
    不上。中心 x 取整段的中点而不是首字——点在名字正中离相邻预设最远。
    """
    ordered = sorted(entries)
    runs: list[list[tuple[int, str]]] = []
    for x, text in ordered:
        if runs and x - runs[-1][-1][0] <= PRESET_WORD_GAP_PX:
            runs[-1].append((x, text))
        else:
            runs.append([(x, text)])
    return [((run[0][0] + run[-1][0]) // 2, "".join(text for _x, text in run)) for run in runs]


def _clickable_hit(runs: Sequence[tuple[int, str]], name: str) -> int | None:
    """这一屏里可以放心点的那个 `name`，没有就 None。

    两道拒绝都**当作「这一屏没有」**而不是报错：往右拖会把落在边距里的名字带进
    安全区，下一屏再点就是了；真到右端还是只有边距里那一个，就走 `PresetNotFound`。

    ⚠️ 边距这道闸眼下**打不着**——`PRESET_NAME_ROI` 的右界是 1000，比
    `PRESET_SAFE_CLICK_MAX_X`（1080）还靠左，真实 OCR 给不出落在边距里的词框。
    留着它是因为那两个数是各自量出来的、会各自变：哪天有人把名字那行的 ROI 往右
    放宽（右边就是第二个预设的数量列，看着很像该放宽），保存按钮立刻就进了候选池，
    而那时唯一还站着的就是这道闸。所以**不要因为「测试构造不出真实场景」删掉它**。
    """
    # ⚠️ **忽略大小写。** 实机 2026-08-13 的日志里 `CCC` 有 118 次被读成 `ccc`
    # （逐屏清单 `[['AAA','探路'], [], ['ccc'], ['ccc']]`），只有 1 次读对。
    # 大小写敏感的匹配意味着：哪天用户把某个任务配成 CCC，这条链路会一发都派不出去，
    # 而报出来的是「预设条上找不到 'CCC'」——看上去像游戏里没有这个预设。
    #
    # 放宽的代价是「aaa」会匹配上「AAA」，而预设名是用户自己起的、就那么几个，
    # 大小写撞名不构成风险；漏匹配的代价则是整条链路停摆。
    wanted = name.casefold()
    for x, text in sorted(runs):  # 命中多个只可能是同名出现两次，取最左那个。
        if wanted not in text.casefold():
            continue
        if PRESET_SAVE_BUTTON_KEYWORD in text:
            continue
        if x >= PRESET_SAFE_CLICK_MAX_X:
            continue
        return x
    return None


def _lanczos(image: Any) -> Any:
    from PIL import Image

    del image
    return Image.Resampling.LANCZOS


def _names_of(entries: Sequence[tuple[int, str]]) -> list[str]:
    return [text for _x, text in entries]


__all__ = [
    "PRESET_NAME_ROI_WIDE",
    "gold_mask",
    "PRESET_NAME_ROI",
    "PRESET_WORD_GAP_PX",
    "PresetNotFound",
    "PresetPicker",
    "merged_names",
    "name_words",
]
