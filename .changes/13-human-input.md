---
issue: 13
agent: root
type: Added
date: 2026-08-07
---

新增 `evo_helper.game.human_input`：采集导航用的拟人化鼠标输入。

- 每次点击都有随机偏移（±4px）、随机移动时长（0.12–0.45s）和随机间隔（0.35–1.10s）。
  像素级精确、固定节奏的连续点击是最明显的自动化特征，本模块不产生这种模式。
- 构造时校验 `pyautogui.FAILSAFE`，为 False 直接拒绝驱动指针——急停（鼠标甩到屏幕角落）
  失效的情况下不允许接管鼠标。
- `drag()` 用于面板内滚动：游戏面板不响应滚轮，只能拖拽。
- **只读护栏**：`FORBIDDEN_LABELS` 拦截 派遣/攻击/删除/领取/取消 及其英文对应词。
  采集流程不得触及这些按钮——最终攻击点击是 ActionGuard 的职责，不能从采集工具到达。
  被拦截时不产生任何指针动作。
- 注入 `seed` 与 `sleep`，使随机化在测试中可复现且不真正等待。

- 配置：无变更
- 数据库：无变更
- 验证：`pytest`（232 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`；
  安全测试覆盖 FAILSAFE 校验、抖动范围、节奏不固定、只读标签拦截
- 安全：本模块只做导航，不含派遣路径；`dry_run` 仍为 true
- 回滚：删除 `human_input.py` 与 `tests/safety/test_human_input.py`
