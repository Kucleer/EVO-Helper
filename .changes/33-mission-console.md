---
issue: 33
agent: root
type: Changed
date: 2026-08-10
---

网页控制台从「建计划 + 定时窗口」改成常驻任务调度器：三条链路拖拽定优先级、
一个开始/结束、扫描恒在最后填空隙。

用户要的不是换个下拉框，而是一个会自己安排次序的东西：优先打满海盗每日 32 次，
然后扫描+攻击 bot，**在等航线的间隙插入全星系扫描**，攻击到点回来收战报再开下一轮。
这条模型成立的前提是扫描**不派遣舰队**（`scan_coordinates.py:10,48`），因此不占航线。

- Configuration: `scheduler_config` 单行——航线上限 / 保留航线 / 日配额 32 /
  最小驻留 60s / 战报宽限 30min / 重启冷却 300s。三条任务的开关与参数在 `mission_tasks`。
  种子默认值：扫描开，海盗与 bot **关**（同 `evo_bot.AUTO_ENABLED` 默认 False 的理由）。
- Database: 新增 `mission_tasks` / `mission_runs` / `scheduler_config`；
  `target_revisits.scope` 加宽到 32（新 scope 名超过原本的 16）。
  `scan_plans` / `run_instances` / `time_window_*` **一列未动**。
- Verification: 913 passed（起点 682）/ ruff / mypy 90 源文件零问题。
  关键判据一律做过变异验证（把实现改坏、确认测试变红、再还原）——
  多处「测试全绿但守不住行为」正是这样暴露的。
- Safety: 任何时刻最多一个子进程点鼠标。为此把 `tools/scan_console.py` 里
  **全仓第二个 runner 启动器拆掉**（见 32），它降级为调度器的瘦客户端。
  权威航线闸门仍在 runner 的 `LineCapacityGate`（它看屏）；调度器的在飞数是
  **乐观估算**，估高了最坏是 runner 空跑一轮就退，不会误派。
  `LiveDriver(allow_actions=)` 的开关位置不变（各 runner 的 `main()`）。
  **整套东西一次实机都没跑过——「开始」按钮从未被点击。**
- Rollback: 三张新表只增不改，回滚删表即可；页面与悬浮窗回到改动前的提交即恢复原样。
  唯一有外部影响的是 `target_revisits.scope` 加宽，回滚需确认没有超过 16 字符的行。
