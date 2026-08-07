---
issue: 9
agent: root
type: Added
date: 2026-08-07
---

新增 `evo_helper.vision.live_reports`，把报告解析原语接成「邮件列表 → 攻击报告详情 → 战斗回放」链路。

- `ReportScreens` 协议按**命名区域**提供 OCR 文本（`mail_rows` / `report_header` / `versus_block` /
  `participating_columns` / `round_columns`）。截图、ROI 几何与滚动留给持有窗口的适配器，
  导航与安全规则因此不依赖浏览器即可测试。
- `LiveReportReader.list_attack_reports` 只返回 `攻击报告`。实测确认邮件二级页签不做过滤，
  且 `海盗攻击报告` 含 `攻击报告` 子串，主题是唯一的区分依据。
- `LiveReportReader.read_report` 产出 `LiveBattleReport`：双方玩家/星球/坐标、UTC 报告时间、
  参战舰队（区分 ship / defence）、逐回合剩余战舰，以及**分别记录**的
  `battle_detail_ui_version` 与 `battle_replay_ui_version`（第 3 节禁止用单一版本号代表整条链路）。

全部 fail-closed，任一条件不满足即抛错而非产出残缺报告——错误的报告会闭合错误的派遣：

- UI 版本未知或不受支持
- 面板仍在渲染（无 `主题` 行、双方舰队均为空）
- VS 块只读到一侧
- 报告时间读不出来
- 主题不是 `攻击报告`
- 回放回合号非从 1 严格递增

- 配置：无变更
- 数据库：无变更
- 验证：`pytest`（143 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`
- 安全：纯只读解析，未新增任何点击路径；`dry_run` 仍为 true
- 回滚：删除 `src/evo_helper/vision/live_reports.py` 与 `tests/unit/vision/test_live_report_reader.py`
