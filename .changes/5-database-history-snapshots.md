---
issue: 5
agent: domain-storage
type: Added
date: 2026-08-06
---

实现数据库、迁移和历史快照：SQLAlchemy 2 模型覆盖 scan_plans、scan_ranges、run_instances、coordinate_scans、bot_targets、attack_intents、attack_dispatches、battle_reports、fleet_snapshots、target_revisits、ui_observations、state_events、artifacts；SQLite 启用 WAL 与外键；Alembic 初始迁移；RepositoryPort 实现（坐标游标领取、扫描/意图/派遣/报告落库、报告严格匹配与一次关闭）；追加式历史与舰队差异计算（新增/减少/消失/首次出现、绝对与百分比变化、总量变化、复查标记、置信度与复核状态）。

- Configuration: 新增 alembic.ini 与 alembic/ 迁移环境；默认库 sqlite:///var/evo-helper.db（var/ 已 gitignore）
- Database: 初始迁移 28376b48e201 创建 13 张表；业务时间统一按 UTC 存取，拒绝 naive 时间戳
- Verification: tests/integration/storage 覆盖游标顺序、幂等、报告匹配、只追加与舰队差异；pytest 45 通过；upgrade→downgrade→upgrade 验证通过
- Safety: 派遣/报告唯一关闭约束在数据库层强制；默认 dry_run=true 未改变
- Rollback: alembic downgrade base 或还原上一次提交
