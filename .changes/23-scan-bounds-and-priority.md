---
issue: 23
agent: root
type: Added
date: 2026-08-07
---

扫描保持可恢复增量模式，跳过 1–4 号位，优先扫 2 系。

- `domain/scan_bounds.py`：`ScanBounds(first_position=5, position_limit=20)`。
  用户确认每恒星系最大 20 位、1–4 恒为海盗，故实际可扫 5–20 共 **16 位**。
- `iter_coordinates` / `next_coordinate_after` 新增 `first_position`：跨恒星系时游标
  回到首个可扫位而不是 1。之前会把 1–4 扫一遍再丢掉，白花 20% 的时间。
  起点低于下限直接报错，不静默上调。
- `galaxy_scan_order()` 把 2 系排在最前，其余升序。**只改顺序不改集合**——
  每个银河系恰好出现一次，所以「优先」不会悄悄变成「只扫」，有测试守着这一点。
- 可恢复性沿用既有机制：`run_instances` 的持久化游标 + `claim_next_coordinate`，
  进程关掉再开能从上次位置接着扫。

**顺带记下一个既有问题**：`coordinates.POSITION_LIMIT` 是 499，但 499 是每银河系的
**恒星系数**，不是每恒星系的行星位数。拿它当位数上限会让游标空转 479 个不存在的位。
位数上限现在由 `scan_bounds.MAX_POSITION` 独立定义，不复用那个常量。

全量规模由常量推导而非估算：9 × 499 = 4,491 恒星系 × 16 位 = **71,856 个坐标**。

- 配置：无变更（`ScanBounds` 与 `galaxy_scan_order` 均可传参覆盖）
- 数据库：无变更
- 验证：`pytest`（448 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`；
  用例覆盖跳位、跨系回到下限、起点越界报错、优先级不改变集合、全量规模推导
- 安全：只影响扫描顺序与范围，不含派遣路径；演习模式仍是默认
- 回滚：删除 `scan_bounds.py` 并撤销 `coordinates.py` 的 `first_position` 参数
