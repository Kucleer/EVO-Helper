---
issue: 283
agent: storage
type: Changed
date: 2026-09-07
---

**`battle_reports.reported_at_utc` 加索引。** 那一列上原先什么都没有。

这个类连 `__table_args__` 都没有，`__table__.indexes` 是空集合，约束只有主键、
`dispatch_id` 的唯一键和外键 —— 生产库里那两个索引
（`battle_reports_pkey` / `battle_reports_dispatch_id_key`）正是这两个约束带出来的。

## 为什么现在加

`docs/任务耗时优化/方案.md` 第 3 节要在**开封每一封邮件之前**按「这个时刻库里有没有
战报」查一次库。没有索引就是每封一次全表扫，而表已经 2969 行、只会长 ——
省下的开封时间会被查库吃回去，而且**分不清是哪一边**。

所以这一条**先单独发**：无行为改动、任何时候都能上，也让后面「跳过」那一步的
耗时变化归得清楚。

## 不止服务那一句探针

这一列本来就有几处按它取数，形状都是这条单列索引吃得到的：

- `repository._unlinked_report_rows` 按它倒序取
- `storage/overview.py` 两处按它取时间窗
- `web/persistent_service.py` 按它正序排

## 为什么是单列，不是「坐标 + 时刻」的复合索引

那句探针**只有时刻**（列表页读不出目标坐标），上面几处也都只按时刻。
复合索引在这些查询上用不上前缀之外的部分，只是多一份写入成本。

- Configuration: 无。
- Database: 迁移 `d3b9f27c4a81`（接在 `c7d92f4a1b60` 后面）。
  `create_index` / `drop_index`，不走 `batch_alter_table`。
- Verification: `ruff check` / `ruff format --check` 过；
  `pytest tests/integration/storage/` 576 passed。
- Safety: 无行为改动。
- Rollback: `downgrade` 删索引。

## 顺带

`test_drop_blind_scrolls_migration.py` 的「head 就是我」降级成「我在链上」——
那句话只该由**最新那一条**的用例说，否则每加一条迁移都要回来改一次，
而改多了就没人再当真。新的 head 断言在 `test_report_time_index_migration.py`。

⚠️ 那份用例里回退目标一律写 `DOWN_REVISION` **不写 `-1`**：等下一条迁移接上来，
`-1` 从 head 往回只走一步，会落在本条**之后**那一条上 ——
`test_ranking_capture_requests_migration.py` 刚这么红过一次。
