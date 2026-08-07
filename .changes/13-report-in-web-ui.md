---
issue: 13
agent: root
type: Added
date: 2026-08-07
---

打通「截图 → OCR → 领域记录 → SQLite → 本地 Web 页面」，报告内容现在能在前端看到。

- `application/report_ingest.py`：`to_battle_report` 把 `LiveBattleReport` 映射为领域 `BattleReport`
  （参战舰队 `round_no=None`，逐回合条目带回合号），`ui_observations_for` 为详情页与回放页
  **各写一条** UI 观察——报告表只有一个 `ui_version` 列，不能代表整条链路（第 3 节）。
  报告以未匹配、未复核状态入库：匹配派遣是仓储的职责，这里不得预先断言置信度。
- `tools/ingest_report.py`：`--detail` / `--replay` 两张截图 → 解析 → 落库，`--dry-run` 只打印不写。
- 侧别常量改为小写 `attacker` / `defender`。仓储与 Web 服务按 `side == "defender"` 精确过滤，
  写成大写会让页面查不到任何快照（本次实测踩到）。

**修正一处 OCR 质量缺陷**：上一轮的离线回归只断言了数量，没断言名称，于是放过了
`无畏舰`→`AKER`、`轰炸机`→`ERR`、`深空吞噬者`→`REBRE`、`MK2 加农炮`→`MKe 加农炮`。
名称是舰队时间线做差异比对的键，名称错乱会让每份报告都显示成「首次出现」。

- 实测 `chi_sim+eng` 数量全对但名称会掉进拉丁噪声，`chi_sim` 名称只差一个字但数量会坏（`5`→`日`）。
  故每列读**两遍**：名称取中文遍，数量取混合遍，按行合并。
- 新增 `snap_unit_name`：舰种是闭集，把 OCR 结果吸附到已知名称（编辑距离 ≤ 1）。
  匹配不唯一、名称短于 3 字或距离过远时，保留原文并标 `unknown`，绝不把新舰种改写成已有的。
- 离线回归补齐名称与分类断言。

- 配置：新增 `.claude/launch.json`（本地起 Web 服务）
- 数据库：无 schema 变更
- 验证：`pytest`（210 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`；
  实测整条链路，17 行舰队的名称与数量与截图逐项一致，合计 2174 相符；
  页面 `/targets/2:149:17` 与 `GET /api/targets/{coordinate}/history` 均正确渲染
- 安全：只读解析与入库，未新增点击路径；`dry_run` 仍为 true
- 回滚：删除 `report_ingest.py`、`ingest_report.py` 与本次测试
