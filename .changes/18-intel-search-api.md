---
issue: 18
agent: root
type: Added
date: 2026-08-07
---

新增情报检索：坐标范围 + 条件树 + 分页排序，全部在服务端过滤。

- `domain/intel_query.py`：条件树（`舰队总数` 与单个舰种，`> >= < <= = !=`，AND/OR 嵌套）。
  纯领域逻辑，自校验并自求值。
- `storage/intel.py`：坐标区间与「每个目标取最新一份战报」下推到 SQL，条件树再交给领域求值器。
  条件不编译成 SQL——候选集已被区间收敛，复用 `ConditionGroup.matches` 才能保证 AND/OR 语义
  只有一份实现，不会和领域测试漂移。
- `POST /api/intel/search`、`GET/POST/DELETE /api/intel/filters`、`GET /api/intel/ships`。
- 持久化「情报筛选器」：名称 + 条件树(JSON) + 可选坐标范围 + 创建/更新时间；迁移 `b7c2d1e40a55`。

按方案要求处理的几个边界：

- **只看防守方**：`side == "defender"` 且 `round_no IS NULL`（参战舰队即战前持有量）。
  逐回合行描述的是每回合幸存数，计入会把每个舰种重复累加。
- **无快照的 bot 不算命中**：`matches(None)` 恒为 False。否则 `钛能守卫者 = 0` 这类条件
  会把所有从未扫描过的 bot 报成命中。不带条件检索时它们仍会列出，并以 `has_fleet_data=false` 标记。
- **只认最新快照**：旧的命中快照不会让目标留在结果里。
- **同星系简写**：`1:100`–`1:200` 展开为 `1:100:1`–`1:200:999`，首尾都包含。
- 校验错误统一 422，消息可直接展示：未知舰种（列出名字）、负数、空条件组、
  不完整/倒置坐标范围、未知运算符、非法游标。

- 配置：无变更
- 数据库：新增 `intel_filters` 表，迁移 `b7c2d1e40a55`；无破坏性变更
- 验证：`pytest`（290 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`；
  方案里的示例查询（`1:100–1:200`、总数 > 2000、钛能守卫者 > 5）有端到端用例
- 安全：只读检索，不含任何派遣路径；修改型接口仍走既有同源/本地令牌校验
- 回滚：撤销迁移 `b7c2d1e40a55`，删除 `intel_query.py`、`intel.py`、`intel_routes.py`
