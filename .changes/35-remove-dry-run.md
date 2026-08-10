---
issue: 35
agent: root
type: Removed
date: 2026-08-11
---

彻底删除「演习模式 / dry_run」这个概念。不是把默认值改成 False，是让这个字段、
这个分支、这个开关、这一列都不再存在——派遣就是真派遣，没有第二条路径。

- Configuration: 删掉 `Settings.dry_run`。`EVO_HELPER_DRY_RUN` 环境变量不再有任何
  作用；`extra="ignore"` 会静默忽略它，不会因此启动失败。`ActionGuard` 随之不再
  需要 `Settings`，构造签名变成 `ActionGuard(*, ttl=..., ...)`。
- Database: 迁移 `a2f6c8d31b70` 用 `batch_alter_table` 删掉 `attack_dispatches.dry_run`
  与 `scan_plans.dry_run`。已在生产库副本上跑过 `upgrade head`：18 张表行数逐张不变，
  `foreign_key_check` / `integrity_check` 干净，`downgrade -1` 再 `upgrade head` 往返正常。
- Verification: `pytest -q` 985 passed / 24 skipped；`ruff check src tests`、
  `ruff format --check src tests`、`mypy src` 全过（即 CI 跑的那四条）。
- Safety: 只删了「`dry_run=true` 一律拒绝派遣」这一条闸。ActionGuard 的一次性
  短时令牌、点击前重新看屏、航线配额闸、预设签名校验、`FORBIDDEN_LABELS`
  一条未动。仓储那几处查询里 `accepted` 那一半过滤**全部保留**——被游戏拒掉的
  派遣同样收不到战报，算进来会变成「已派出且永远收不到战报」的死记录。
- Rollback: `alembic downgrade -1` 把两列加回来（派遣回填 0、计划回填 1），再回退代码。
  回填值只还原形状，不还原原始数据。
