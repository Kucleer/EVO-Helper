"""海盗侦察/攻击链路的界面坐标与判据。**每个常量都是实机点通过的**（2026-08-09）。

坐标是 client 空间，标定视口 1920×917（游戏窗口 1938×926，含 38px 标题栏）。
改动之前请用 `tools/capture.py` 重新拍一张核对，不要照抄文档。

## 实机走通的链路

    行星面板（敌对海盗）
      → 点「侦察」  → 派遣面板（自动预选 探测器 ×1，终点自动填成该海盗坐标）
      → 点 绿✓      → 简报页（任务类型: 侦察）
      → 点「出发！」 → 回到「飞行中」列表

攻击链路同形，区别只在第一步点「攻击」、以及派遣面板上要先选预设。

## 三个实机上撞出来的坑

- **派遣面板的终点是自动预填的，而且是对的**。曾经怀疑它沿用旧值，
  复核后发现是自己导航停在了别的位号——不是游戏的问题。仍然建议派出前回读一次。
- **预设条是连续横向滚动，不是分页**，一屏只看得见约两个预设。
  所以选预设必须「OCR 定位 → 点」，不能写死第 N 个的坐标。
- **预设条最右端是「+ 保存当前舰队」**。拖过头点到它会新建/覆盖预设，
  是这条链路上唯一一个会改坏用户配置的控件。拖动必须留出右边距。
"""

from __future__ import annotations

from evo_helper.domain.text import snap_to_vocabulary

#: 行星面板上「敌对海盗」的标题与坐标行。两者都读到才算认出是海盗位。
PIRATE_TITLE_ROI = (760, 350, 1160, 392)
PIRATE_COORD_ROI = (850, 396, 1070, 426)
PIRATE_TITLE_TEXT = "敌对海盗"

#: 行星面板上的两个动作按钮。
SCOUT_BUTTON = (886, 540)
ATTACK_BUTTON = (1032, 540)

#: 派遣面板：关闭、确认目标（绿 ✓）、预设条展开/收起。
DISPATCH_CLOSE = (750, 71)
DISPATCH_CONFIRM = (1156, 763)
PRESET_TOGGLE = (1176, 646)

#: 派遣面板的终点三字段，派出前回读用。
DESTINATION_GALAXY_ROI = (965, 698, 1055, 728)
DESTINATION_SYSTEM_ROI = (965, 746, 1055, 776)
DESTINATION_POSITION_ROI = (965, 794, 1055, 824)

#: 预设条展开后：预设名所在的行，以及可安全拖动的横向范围。
#: 右端留出 120px 是为了避开「+ 保存当前舰队」——点到它会改坏用户的预设。
PRESET_NAME_ROW_Y = 693
PRESET_STRIP_ROI = (725, 680, 1200, 770)
PRESET_DRAG_Y = 760
PRESET_DRAG_FROM_X = 1150
PRESET_DRAG_TO_X = 800
PRESET_SAVE_BUTTON_MARGIN_PX = 120

#: 一次拖动大约推进的像素；用来判断「拖到底了」而不是无限拖下去。
PRESET_MAX_DRAGS = 8

#: 简报页（点「出发！」之前的最后一屏）。
#:
#: ⚠️ 任务类型的 ROI **必须避开左边那个任务图标**。图标框进来之后，
#: `chi_sim+eng` 会把「攻击」连同图标一起读成 `'6 me'` 之类的噪声，
#: 于是「简报上写的是不是攻击」这道闸门在一次完全正常的攻击上误判为不通过。
#: 实机撞到过：文字左边缘在 x≈1075，图标占到 x≈1060。
BRIEFING_MISSION_ROI = (1075, 336, 1185, 374)
BRIEFING_FLIGHT_ROI = (1050, 452, 1210, 482)
BRIEFING_ARRIVAL_ROI = (1050, 490, 1210, 540)
BRIEFING_BACK_BUTTON = (841, 815)
BRIEFING_LAUNCH_BUTTON = (1077, 815)

#: 底部导航。
#:
#: ⚠️ **(999, 862) 是「太空舱」，不要点。** 它的图标是个信封，看起来像信箱，
#: 实机上点开却是材料仓库（暗能量/合金碎片/银河素…），而且会把整条导航条盖住，
#: 还得再关一次。标定时在这里浪费过一轮，所以这个坐标**故意不定义成常量**——
#: 定义了就迟早有人点。真正的信箱在导航条右翻之后，坐标待标定。
NAV_PLANET = (840, 862)
NAV_FLEET = (920, 862)
NAV_SCROLL_RIGHT = (1204, 862)

#: 判定门槛：侦察报告里这几种舰船任一数量超过这个值，就用攻击预设打。
#: 门槛低是用户明确确认过的——几乎每个有舰队的海盗都会命中。
PIRATE_TRIGGER_SHIPS = (
    "深空吞噬者",
    "噬能截击者",
    "钛能守卫者",
    "收割者",
)
PIRATE_TRIGGER_MIN_COUNT = 1

#: 攻击用的游戏内预设（实机核对 2026-08-09，预设槽 4/10）。
ATTACK_PRESET_NAME = "AAA"
ATTACK_PRESET_COUNTS = {"深空吞噬者": 70, "钛能守卫者": 30}


#: 简报上可能出现的任务类型，封闭集合。
MISSION_LABELS = ("攻击", "探索", "运输", "回收", "侦察", "殖民", "部署")


def snap_mission(raw: str, *, max_distance: int = 1) -> str | None:
    """把 OCR 出来的任务类型贴回封闭集合；贴不上或有歧义就返回 None。

    实机上「攻击」被 `chi_sim` 读成过「政击」——差一个字。直接做子串判断会漏，
    而放宽成「含『击』就算攻击」又会让别的任务蒙混过关：闸门的意义就在于
    **选错任务类型时拦住**，松掉它等于把闸门拆了。

    所以按编辑距离贴，且**要求唯一命中**：两个候选并列时宁可判不出来也不猜。
    """
    return snap_to_vocabulary(raw, MISSION_LABELS, max_distance=max_distance)


def briefing_says_attack(raw: str) -> bool:
    """派攻击之前的最后一道闸门。贴不出来一律当作「不是攻击」。"""
    return snap_mission(raw) == "攻击"


def triggers_attack(ships: dict[str, int]) -> bool:
    """侦察到的舰队是否够格挨一发。

    「任一 > 1」是用户确认过的字面规则。注意这**不是**强弱判断——
    它只问「这个海盗有没有实打实的舰队」，不问打不打得过。
    """
    return any(ships.get(name, 0) > PIRATE_TRIGGER_MIN_COUNT for name in PIRATE_TRIGGER_SHIPS)


__all__ = [
    "ATTACK_BUTTON",
    "ATTACK_PRESET_COUNTS",
    "ATTACK_PRESET_NAME",
    "BRIEFING_LAUNCH_BUTTON",
    "BRIEFING_MISSION_ROI",
    "DISPATCH_CONFIRM",
    "PIRATE_TRIGGER_SHIPS",
    "PRESET_TOGGLE",
    "SCOUT_BUTTON",
    "triggers_attack",
]
