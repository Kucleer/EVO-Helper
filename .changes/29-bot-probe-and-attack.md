---
issue: 29
agent: vision-game
type: Added
date: 2026-08-09
---

bot 目标的「攻击侦查 → 分档 → 攻击」自动化。

```bash
python -m evo_helper.tools.bot_loop --targets 2:137:14              # 只认目标，不派
python -m evo_helper.tools.bot_loop --targets 2:137:14 --probe      # 用「探路」打一发侦查
python -m evo_helper.tools.bot_loop --targets 2:137:14 --probe --attack
```

## 设计要点

- **与海盗链路的区别只在判定依据。** 海盗看侦察报告里几个特定舰种的数量（有没有舰队），
  bot 看攻击侦查打回来的战报里守方的「单位」总数，按 `domain.fleet_tier` 分三档。
  导航、简报闸门、选预设、写 intent/dispatch 全部复用 `pirate_loop.PirateLoop`。
- **分档只读「单位」总数，不读逐舰种明细。** 分档防的是量级错，不是末位误差；
  总数是详情页上独立的一个数，一个 ROI 就读到。明细要进回放页、读两列、
  还要重拍到合计对上，对分档没有增量价值。
- **各档的预设标题改成游戏里真实存在的那几个**（用户确认：甲=AAA、乙=BBB、丙=CCC）。
  原先写的是「攻击组合甲/乙/丙」——游戏里没有这些预设，`PresetPicker` 按标题找一定
  找不到，于是每一发都会在「找不到预设」上整发放弃。**改这里之前对着预设条核标题。**
- **2K 以下不派**（`FleetTier.NEGLIGIBLE.preset is None`）。
- 找战报靠 VS 块里的**目标坐标**核对，不靠行号：行序随新邮件变，
  而报告自己写着打的是谁。

- 配置：无变更
- 数据库：无变更（复用 `attack_intents` / `attack_dispatches`）
- 验证：`pytest`（690 passed）、`ruff format --check`、`ruff check`、`mypy`；
  实机 `--targets 2:137:14 2:149:17`（只认目标）两个都认出、0 次拒绝
- 安全：默认不派任何东西；`--attack` 需要 `--probe`（没有战报就没有分档依据）；
  出发前仍是「面板认得出目标」「预设标题选中」「简报写着攻击」三道闸门
- 回滚：删除 `tools/bot_loop.py`
