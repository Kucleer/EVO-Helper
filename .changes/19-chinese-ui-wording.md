---
issue: 19
agent: root
type: Changed
date: 2026-08-07
---

界面文案全部改为中文，去掉会引起歧义的英文。

- `dry run` → **演习模式**。悬停说明写清含义：扫描、识别、记录照常，只是不点最终派遣。
- 运行状态不再直接显示英文常量，改为中文标签（`run_state_label`）：
  `ARMED` → 待命、`SCANNING` → 扫描中、`WAITING_CAPACITY` → 等待航线、
  `DRAINING` → 收取战报、`PAUSED` → 已暂停、`EMERGENCY_STOPPED` → 已紧急停止 等。
  未知状态回落到原值——宁可显示英文，也不要显示空白。

代码标识符 `dry_run`、接口字段与数据库取值保持不变：它们不是给人看的，
改名会破坏接口且不会减少任何歧义。这次只动用户能看到的文字。

- 配置：无变更
- 数据库：无变更
- 验证：`pytest`（339 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`；
  新增用例断言三个页面都不含 `dry run` 字样且含「演习模式」，以及状态标签的中文映射与回落；
  浏览器实测运行详情页显示「演习模式 已锁定」与「已紧急停止」
- 安全：纯文案，判定逻辑不变；演习模式仍是默认且界面无开关
- 回滚：还原模板文案与 `run_state_label`
