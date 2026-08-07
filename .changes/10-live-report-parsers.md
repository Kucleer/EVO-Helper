---
issue: 10
agent: root
type: Changed
date: 2026-08-07
---

按 2026-08-07 实测的当前 UI 布局补齐报告解析原语，并修掉两处会静默产出错误坐标的缺陷。

新增：

- `parse_report_timestamp`：解析 `DD/MM/YYYY HH:MM:SS`（游戏实际格式，此前只支持 ISO）。`display_zone` 为必传参数，不设默认值——游戏渲染报告时间所用时区尚未核实，默认一个时区会让每份报告整点偏移并破坏严格匹配。
- `classify_report_subject` / `ReportKind`：区分 `攻击报告` / `海盗攻击报告` / `侦察报告` / 系统战报，只有 `攻击报告` 的 `is_dispatch_matchable` 为真。
- `parse_fleet_column`：解析「名称 + 空白 + 数量」两列布局；显式的 `0` 保留为一行；`FleetLine.category` 区分 `ship` / `defence` / `unknown`，未识别名称不猜测归类。
- `parse_versus_block`：从 VS 块解析双方玩家、星球与坐标，任一侧不完整则返回 `None`。
- `parse_mail_rows_v2`：解析主题/发件人/时间行；实时列表不含坐标，`coordinate` 恒为 `None`。
- `parse_replay_rounds`：按回合解析剩余战舰，回合号必须从 1 严格递增，乱序或重复直接报错。

修复：

- `parse_battle_detail` / `parse_battle_replay` 在读不到两个坐标时会退回占位坐标 `1:1:1`（一个真实坐标），或把一侧坐标同时当作双方。现改为抛 `UnknownUiVersionError` 安全停止。
- 同一行上的两个坐标（`1:2:3 -> 9:8:7`）此前只取到第一个，防守方坐标被丢弃；新增 `parse_all_coordinates` 按顺序取全部。

- 配置：无变更
- 数据库：无变更
- 验证：`pytest`（125 passed，本地跳过依赖 httpx2 的 web 用例）、`ruff check src tests`、`ruff format --check src tests`、`mypy src` 全部通过
- 安全：全部为 fail-closed 收紧；未新增任何点击路径，`dry_run` 仍为 true
- 回滚：还原 `src/evo_helper/vision/parsers.py`、`models.py` 与 `tests/unit/vision/test_live_report_parsers.py`
