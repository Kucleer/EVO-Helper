---
issue: 19
agent: root
type: Added
date: 2026-08-07
---

记录读取一份邮件（攻击报告）所需的时间。

- `LiveReportReader.read_report` 现在按阶段计时：`header` / `versus` / `fleet` / `rounds`，
  并把 `ReadTiming` 挂在返回的 `LiveBattleReport` 上。
  只给总时长只能说明「慢」，分阶段才能指出该优化哪一次 OCR 调用。
- 成功时 `logger.info`，**失败时也记录已耗时**并 `logger.warning` —— 一次跑了三十秒才失败的读取，
  正是这条日志要抓的东西。
- 时钟通过 `clock` 注入，随机化与真实等待在测试中可完全避开。
- 新增 `infrastructure/logging.py`：`evo_helper` 日志落到 `var/logs/report-read.log`
  （2MB 轮转、保留 5 份）并同时输出到控制台。重复调用会替换而非叠加 handler。
- `evo-ingest-report` CLI 打印本次读取用时、最慢阶段与日志路径。

**实测**：单份报告 2.81s —— `header 0.41s, versus 0.67s, fleet 1.73s, rounds 0.00s`。
舰队列占了六成，因为名称/数量分两遍 OCR，两列共四次调用。这是后续优化的第一目标。

- 配置：无变更
- 数据库：无变更
- 验证：`pytest`（337 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`
- 安全：只读计时，不影响任何判定路径
- 回滚：删除 `ReadTiming`、`_StageTimer` 与 `infrastructure/logging.py`
