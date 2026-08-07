---
issue: 19
agent: root
type: Fixed
date: 2026-08-07
---

迁移不再关闭应用已配置的日志。

`alembic/env.py` 调用 `fileConfig(...)` 时用的是默认的 `disable_existing_loggers=True`，
会禁用此前已存在的所有 logger。而 Web 运行时**在启动时执行迁移**，因此这一步会把
`evo_helper` 下的日志（包括新加的报告读取计时）在整个进程剩余生命周期里静音。

改为 `disable_existing_loggers=False`。这个缺陷是加计时日志时被测试串扰暴露出来的
——单跑通过、全量跑失败——但它在生产路径上同样存在。

- 配置：无变更
- 数据库：无变更
- 验证：`pytest`（337 passed）；新增回归用例断言 `_upgrade_database` 之后
  `evo_helper.vision.live_reports` 仍然启用
- 安全：无影响
- 回滚：还原 `fileConfig` 调用
