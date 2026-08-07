---
issue: 6
agent: root
type: Added
date: 2026-08-07
---

记录 2026-08-07 当前 UI 的字段结构与实时样本：邮件列表 v2、攻击报告详情、战斗详情段、战斗回放，以及两种加载中状态。

- 配置：无变更
- 数据库：无变更
- 数据集：批次 `evo-20260807-live`，4 份样本，1920×879，单一会话，来源 `user-manual-screenshot`；清单 `var/captures/evo-20260807-live/evo-20260807-live-manifest.json` 通过 `dataset validate --capture-evidence`；原图受 `.gitignore` 保护不入库
- 验证：`datasets/manifests/live-ui-observations-20260807.json`；导航路径 home → 邮件 → 报告 → 攻击报告 → 战斗详情 → 战斗回放，全程只读
- 安全：未删邮件、未领奖励、未派遣舰队；`dry_run` 仍为 true。样本量与会话数不满足第 7.4 节验收门槛，`docs/ui-version-matrix.md` 维持「Needs current samples」
- 回滚：删除该清单文件与 `var/captures/evo-20260807-live/`
