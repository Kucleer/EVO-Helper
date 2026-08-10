---
issue: 34
agent: vision-game
type: Fixed
date: 2026-08-10
---

侦察发的航线释放时刻补上：`scout()` 现在也读简报上的飞行时长。

`34-dispatch-line-clock.md` 给侦察发补了 intent + dispatch + `mission_kind=SCOUT`，
但没读飞行时长，于是 `line_free_at_utc` 恒为 NULL。而 NULL 的既定语义是
**不计入在飞数**——记了账等于没记：海盗一轮最多 4 发侦察，这 4 条航线对调度器
仍然完全隐形，它以为航线空着就去派攻击，撞上游戏弹窗
「同时派遣的舰队数量已达上限。」（用户实机撞到的正是这个提示）。

现在 `scout()` 在点「出发！」**之前**调 `_read_flight_time()`，位置与顺序和
`attack()` 完全一致，读到的值经 `_record_dispatch(..., mission_kind=SCOUT)`
落到 `line_free_at_utc`（侦察 ×2，探测器会飞回来）。

侦察简报是同一块面板（只是「任务类型」显示为侦察），ROI 沿用
`BRIEFING_FLIGHT_ROI`。读不出来照既有语义写 NULL、**不拦这一发**：飞行时长是
闹钟不是闸门，加闸门会让一次 OCR 抖动杀掉一发健康的派遣（这条链路为此白白
拦下过四发攻击）。

- Configuration: 无。
- Database: 无迁移。侦察发的 `line_free_at_utc` 从此有值，存量行不回填
  （`flight_seconds` 本来就是 NULL，无从回算）。
- Verification: 940 passed（起点 937）/ ruff `src tests` 全绿 / mypy 90 源文件零问题。
  两处变异各验过一次（改坏 → 测试变红 → 还原）：① `scout()` 不读飞行时长
  （回到改动前）→ 2 条红；② 把读取挪到 `_launch()` 之后 → 顺序那条红。
  第二处只有顺序断言拦得住——测试里的假屏幕不会随「出发！」消失，
  取值断言照样绿，这与 `attack()` 那边同形。
- Safety: `scout()` 里既有的点击、等待、认屏一步未动，新增的只有一次**只读**的
  OCR（`_read_flight_time()` 不含任何 `click`）；点击顺序断言把这件事钉住。
  权威航线闸门仍是 runner 里看屏的 `LineCapacityGate`。`pyautogui.FAILSAFE` 未动。
- Rollback: 去掉 `scout()` 里那一行读取即可，行为退回本次改动之前。
- 实机第一次要留意: 若 `BRIEFING_FLIGHT_ROI` 在侦察简报上对不上，读不出来会先走
  `_read_flight_time` 里 `_settle` 的重试（约 3 秒），`_launch` 里还会再走一遍，
  于是**每发侦察多花约 6 秒、一轮 4 发约 24 秒**。那是 ROI 没对上的症状，
  不是别的毛病——同一句话也写在 `scout()` 的行内注释里。
