---
issue: 19
agent: root
type: Added
date: 2026-08-07
---

扫描任务新增航线数量配置，并支持保留自由航线。

- `scan_plans` 新增 `fleet_line_limit`（本任务最多占用的舰队航线数）与
  `reserved_lines`（始终留给用户自己派遣、助手永不占用的航线数）；迁移 `c3f81a97b2d4`，
  既有计划取 `1 / 0`，行为不变。
- `LineCapacityGate(user_limit, reserved_lines=...)`：可用上限 = 上限 − 保留。
  保留航线还必须扛得住游戏自身的空位反馈——游戏报 3 个空位而保留 2 条时只允许占用 1 条，
  助手不会去拿最后那条保留位。
- **保留数必须小于上限**，否则任务永远无法派遣；前端与两个服务端实现都拦截。
- 任务中心表单新增两个字段与说明，历史任务列表新增「航线 / 保留」两列。

- 配置：无变更
- 数据库：新增两列，迁移 `c3f81a97b2d4`；有 server_default，无破坏性变更
- 验证：`pytest`（337 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`；
  容量门禁新增 8 条用例覆盖保留位与游戏反馈的交互；
  浏览器实测建成「上限 6 / 保留 2」的计划，可用 4 条；全保留时前后端各自拒绝
- 安全：只会**收紧**可派遣的航线数，不会放宽；`dry_run` 仍为 true
- 回滚：撤销迁移 `c3f81a97b2d4` 并还原 `LineCapacityGate` 签名
