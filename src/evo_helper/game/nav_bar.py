"""底部导航条：几何、词表、读法。**只有这一份。**

## ⚠️ 这条条是**可横向滚动**的，而两条链都点它

- 军力榜链为了露出「排名」，把条往左拖（`NAV_DRAG_FROM_X` → `NAV_DRAG_TO_X`）。
- 攻击链要点「行星」开行星列表、点「舰队」回读起点。

两边读的是**同一条条、同一个 `NAV_LABEL_ROI`、同一套 OCR 配方、同一个合并阈值**。
所以它们住在这里，而不是各自抄一份。

⚠️ **这一点和 `merged_labels` 与 `preset_picker.merged_names` 那条「同形而不共用」
的先例不冲突，判据是同一个：两处读的是不是同一块像素。** 预设条和导航条是两块不同
的横条（字距 10 / 项距 237 对 项距 80），两个阈值各自会变，所以分开；而这里只有一
条条、一份真相，分开写就是造第二份。分开的失败还是**静默**的：哪天版面下移 10px，
改了军力榜那份、没改攻击链那份，症状只是「本轮不派」，没人会想到去看另一条链。

## ⚠️ 2026-08-24 的事故：写死的像素在条滚动之后指向别的东西

拖之前那一屏的实测词框是 `行星 839 · 舰队 920 · 太空 993 · 舱 1017 · 商店 1081`，
而攻击链点的是写死的 `pirate_ui.NAV_PLANET = (840, 862)` 与 `NAV_FLEET = (920, 862)`
——**正好对着「行星」和「舰队」**。条被军力榜拖到右段之后，同样这两个像素底下换成了
别的项：那一天生产上点出来的是**太空舱**面板（用户实机确认）。

而太空舱面板会把整条导航条连同行星列表一起盖住，于是形成一个自维持的闭环：
点 (840,862) → 开出太空舱 → 盖住条 → 标签读不出 → 关掉浮层重试 → **又点 (840,862)**。
当天「行星列表坐标 OCR 全空」出现 **25 次**，每次都以「这一轮一发都不派」收场。

⇒ 所以要点这条条上的东西，**先读一次标签、拿实际的 x 去点**，别信写死的像素。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from evo_helper.domain.text import snap_to_vocabulary

#: 导航条图标那一行的 y。**点击落在这一行**，OCR 读的是它下面的标签行。
#:
#: ⚠️ 横向滚动不影响它：标签在 880–906、点击落在图标行 862，这个纵向关系是版面
#: 定的。滚动只改 x。
NAV_BAR_Y = 862

#: 往左拖（内容左移、露出右段）的起止 x。实测拖完就出 `太空舱 商店 联盟 排名 设置`。
NAV_DRAG_FROM_X = 1122
NAV_DRAG_TO_X = 860

#: 拖完等惯性停下。
NAV_DRAG_WAIT_S = 1.2

#: 拖动次数上限。实测**一次**就够；留 2 次是给「打开时条停在更左边」留的余量，
#: 而不是给「拖不动就多拖几下」留的——拖不动时多拖一百次也一样。
NAV_MAX_DRAGS = 2

#: 标签行的 ROI（整窗坐标）。
#:
#: ⚠️ **和 `system_navigator.NAV_LABEL_ROI` 同名不同物**：那个是**顶部**恒星系
#: 导航栏 (740, 88, 1190, 115)，这个是**底部**导航条。两者都被 `pirate_loop`
#: import 过，接线时必须起别名，否则是一次静默的张冠李戴。
NAV_LABEL_ROI = (760, 880, 1220, 906)

#: 标签行 OCR 的放大倍数。
NAV_LABEL_UPSCALE = 3

#: 整条导航条上的**封闭词表**——两段都在里面。
#:
#: ⚠️ **2026-08-24 从五个扩成七个**：原先只有拖完之后那一段（`太空舱 商店 联盟
#: 排名 设置`），于是攻击链要找的「行星」「舰队」贴不回词表。而那两个正是基线段
#: 独有的标签，也是判断「条在哪一段」唯一可靠的凭据——
#: **「商店」两段都有（918 与 1081），拿它判不出来。**
#:
#: ⚠️ 扩容之后 `NAV_LABEL_MAX_DISTANCE = 1` 那条账原样成立：七个条目**两两编辑
#: 距离全部 ≥ 2**（六个二字词彼此无同字 → 2；三字的「太空舱」对任何二字 → 3）。
#: 唯一的变化是多了两个可能与某次误读并列的候选，而并列时 `snap_to_vocabulary`
#: 判歧义返回 `None` ——方向是「更容易认不出」，正是那条注释想要的安全侧。
#: `test_nav_bar.py` 里有一条属性测试钉住这个距离，将来谁加一个「行动」
#: （离「行星」1）进来会当场红。
NAV_LABELS = ("行星", "舰队", "太空舱", "商店", "联盟", "排名", "设置")

#: 只在**基线段**出现的两个标签——读到它们就等于「条在基线上」。
PLANET_LABEL = "行星"
FLEET_LABEL = "舰队"

#: 只在**右段**出现的两个标签——读到它们就等于「条被拖到右段了」，确凿。
RIGHT_SEGMENT_LABELS = ("排名", "设置")

#: 贴回词表时允许的编辑距离。
NAV_LABEL_MAX_DISTANCE = 1

#: 合并相邻词框的阈值（像素）。tesseract 对中文按字切词，不合并的话「排名」永远
#: 只读到「排」或「名」，贴不回词表。
#:
#: ✅ 2026-08-14 实机量到了：拖之前那一屏的原始词框是
#: `行星 839 · 舰队 920 · 太空 993 · 舱 1017 · 商店 1081 · 联盟 1161`，
#: 于是两个真实字距都拿到了。真正的上界是 57 而不是 80——按中心距 80 挑阈值会把
#: 「联盟」和「排名」合成一个 `联盟排名`。40 落在 24 与 57 中间。
NAV_LABEL_WORD_GAP_PX = 40

#: 读到的标签 x 与标定像素差多少就记一条 WARNING。**它不拦动作**，只是让「版面漂了」
#: 在库里留下痕迹。
#:
#: 取 20 的账：远大于 OCR 词框中心的抖动（1–2px）加点击抖动（`CLICK_JITTER_PX` ±4），
#: 远小于相邻项距 80 的一半——落在这两者之间，既不会天天误报，也不会把「点到隔壁项」
#: 放过去。
NAV_LABEL_HOME_TOLERANCE_PX = 20


def merged_labels(entries: Sequence[tuple[int, str]]) -> list[tuple[int, str]]:
    """把靠得足够近的相邻词框合成一个标签，返回 `(中心 x, 完整标签)`。

    见 `NAV_LABEL_WORD_GAP_PX`：tesseract 对中文按字切词，不合并的话「排名」永远
    只读到「排」或「名」，贴不回词表，于是永远找不到。

    中心 x 取整段的中点而不是首字——点在标签正中离相邻的导航项最远。

    与 `preset_picker.merged_names` 同形而不共用：那边的阈值是在预设条上量的
    （字距 10 / 项距 237），这边是在导航条上量的（项距 80）。两个数各自会变，
    共用一个就会出现「改了预设条的容差，导航条跟着认错」。
    """
    ordered = sorted(entries)
    runs: list[list[tuple[int, str]]] = []
    for x, text in ordered:
        if runs and x - runs[-1][-1][0] <= NAV_LABEL_WORD_GAP_PX:
            runs[-1].append((x, text))
        else:
            runs.append([(x, text)])
    return [((run[0][0] + run[-1][0]) // 2, "".join(text for _x, text in run)) for run in runs]


def label_x(runs: Sequence[tuple[int, str]], wanted: str) -> int | None:
    """这一屏里 `wanted` 那个标签的中心 x，没有就 `None`。

    **贴回封闭词表**（`NAV_LABELS`）而不是做子串判断：实机上 `chi_sim` 把「攻击」
    读成过「政击」、把「派遣」读成过「派遗」，差一个字 `in` 就直接漏掉。而放宽成
    「含『名』就算」又会让别的项蒙混过关。`snap_to_vocabulary` 要求**唯一命中**，
    两个候选并列时判不出来而不是猜。

    ⚠️ 落在标签行 ROI 之外的 x 一律不当候选。眼下这道闸**打不着**——x 是从 ROI
    裁出来的图上换算回来的，真实读数出不了界。留着它是因为 ROI 和「用什么坐标去点」
    是两件各自会变的事：哪天有人改成整窗 OCR，这道闸就是唯一还站着的东西。
    **不要因为「测试构造不出真实场景」删掉它**（同 `preset_picker._clickable_hit`）。
    """
    for x, text in sorted(runs):
        snapped = snap_to_vocabulary(text, NAV_LABELS, max_distance=NAV_LABEL_MAX_DISTANCE)
        if snapped != wanted:
            continue
        if not NAV_LABEL_ROI[0] <= x <= NAV_LABEL_ROI[2]:
            continue
        return x
    return None


def nav_label_words(image: Any, ocr: Any) -> list[tuple[int, str]]:
    """从一张整窗截图里读出底部导航标签行的 `(中心 x, 文字)`。

    与 `preset_picker.name_words` 同形（那边找预设、这边找导航项），配方是实机
    2026-08-14 用的那一套：`chi_sim`、`--psm 6`、3×。用词框而不是整行文本：
    要拿 x 去点。

    ⚠️ 只跑 `chi_sim` 不跑 `eng`：这些标签全是中文，掺进 `eng` 只会多一份把「排」
    认成字母的机会。
    """
    crop = image.crop(NAV_LABEL_ROI).convert("L")
    grey = crop.resize(
        (crop.width * NAV_LABEL_UPSCALE, crop.height * NAV_LABEL_UPSCALE),
        _lanczos(),
    )
    data = ocr.image_to_data(grey, lang="chi_sim", config="--psm 6", output_type=ocr.Output.DICT)
    words: list[tuple[int, str]] = []
    for index, word in enumerate(data["text"]):
        text = word.strip()
        if not text:
            continue
        left = NAV_LABEL_ROI[0] + data["left"][index] // NAV_LABEL_UPSCALE
        width = data["width"][index] // NAV_LABEL_UPSCALE
        words.append((left + width // 2, text))
    return words


def _lanczos() -> Any:
    from PIL import Image

    return Image.Resampling.LANCZOS


__all__ = [
    "FLEET_LABEL",
    "NAV_BAR_Y",
    "NAV_DRAG_FROM_X",
    "NAV_DRAG_TO_X",
    "NAV_DRAG_WAIT_S",
    "NAV_LABELS",
    "NAV_LABEL_HOME_TOLERANCE_PX",
    "NAV_LABEL_MAX_DISTANCE",
    "NAV_LABEL_ROI",
    "NAV_LABEL_UPSCALE",
    "NAV_LABEL_WORD_GAP_PX",
    "NAV_MAX_DRAGS",
    "PLANET_LABEL",
    "RIGHT_SEGMENT_LABELS",
    "label_x",
    "merged_labels",
    "nav_label_words",
]
