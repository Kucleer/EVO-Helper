---
issue: 4
agent: domain-storage
type: Added
date: 2026-08-06
---

实现坐标范围与任务状态机：坐标解析与字典序迭代、UTC+8 时间窗口调度（窗口前 ARMED 等待、窗口内立即扫描、窗口后预约次日）、跨日预约、DRAINING 语义、周周期去重（周一 00:00 UTC 起算）、强制复查绕过去重、幂等启动键校验。

- Configuration: 无新增运行时配置；扫描窗口沿用默认 08:00–20:00（UTC+8）
- Database: 无变更
- Verification: tests/unit/domain 覆盖窗口边界、跨日、去重、强制复查与幂等键；pytest 45 通过
- Safety: 状态转换仍必须由应用服务触发并记录 state_events；干跑默认不变
- Rollback: 还原上一次 domain 提交即可
