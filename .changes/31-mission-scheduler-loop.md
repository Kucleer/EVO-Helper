---
issue: 31
agent: root
type: Added
date: 2026-08-09
---

常驻任务调度器的第二段：子进程管理 + 调度循环 + 接进控制台。三条任务链路共享一个
游戏窗口和一个鼠标，任何时刻只能有一个子进程在点。

## 加了什么

- **`application/mission_supervisor.py`**：同时只管一个子进程。照
  `tools/scan_console.py` 的 `ScanSupervisor` 长，但**去掉自动续跑**——那是扫描
  链路的特性（扫描不派遣、断在哪都能接着扫），攻击类任务自己重启会连着再派一轮
  舰队，一天 32 次配额可以在没人看着的时候悄悄打光。`stop()` 立刻
  `terminate() + wait(5)`，不等它跑完手上这一个；`wait` 超时也把状态清干净，
  否则控制台永远停在「运行中」，谁也起不来。真正 `Popen` 的函数单独放，
  测试一律注入假的。

- **`domain/scheduler.py` 补 `RESTART_COOLDOWN`（默认 5 分钟）**。它堵的是
  「立即收取」的空转：`expected_report_at_utc` 为 NULL 时战报判据恒为「该去收」，
  而战报可能只是还没到（同系短程飞行按分钟计）。runner 进信箱、扑空、退出、
  下一 tick 判据仍为真、再起一次——不是死循环，但每轮几十秒的导航全白费，还一直
  占着鼠标不让扫描进来。冷却期内该 kind 视为「没活干」，顺位让给下一个；抢占那一
  路复用同一个 `has_work()`，所以冷却中的链路也不会把扫描打断成谁都不在跑。
  「上次启动时刻」作为事实进 `SchedulerFacts`，这一层仍然不碰 IO。

- **`domain/scheduler.py` 补 `quota_day_start_utc()`**。重置点是 UTC 00:00
  （本地 UTC+8 的早上 8 点）。做成具名纯函数，是因为调用方一旦自己写
  `replace(hour=0)`，那个 `replace` 落在本地时刻上就悄悄变成本地日历天，而两者
  只在一天里的某几个钟头对得上：本地 0–8 点会**漏数**当日 UTC 00:00–16:00 的
  派遣（以为还有额度 → 舰队被强制返回，白飞一趟），之后又会**多数**昨天尾巴上
  的（配额提前判成用尽 → 白白少打几次）。

- **`application/mission_scheduler.py`**：完整的调度循环。读事实 → `decide()` →
  起 / 抢占 / 空转 → 写 `mission_runs`。连续 3 次异常退出自动停用（没有这条，
  调度循环会在坏掉的任务上变成满速空转的重启循环）；抢占和用户点停**不算失败**。
  `MissionParamError` 就地停用该任务并让位给下一个，绝不冒出去打死整个循环。
  bot 的完成判据走 `bot_dispatch_facts()` + `phase_of()` 逐个目标判，`DONE` 的
  不计入剩余。不参与调度的链路一律不查库——bot 要按目标逐个问，而 tick 每秒一次。

- **接进 `web/app.py` 的 lifespan**：开机补齐三行任务与单行配置、把 `ended_at_utc`
  为空的 `mission_runs` 标成 `UNKNOWN`（**只标不杀**：pid 会被系统回收复用，照着
  一个可能已经换了主人的号码开枪比留个警告更糟）；每秒一次 `tick()` 走
  `to_thread`（停子进程要 `wait(5)`，放在事件循环里会把整个控制台连页面一起卡住）；
  关闭时 `shutdown()` 主动清子进程。**调度器开关不持久化**，重启后一律停在
  「已停止」。

- **三行任务与单行配置的初始化**（波次 1 评审点名的洞）：迁移里没有 `bulk_insert`，
  仓储里没有 upsert。改为每次开机对一遍，只补不改——第二遍要是覆盖，用户拖出来的
  优先级每次重启都被抹掉。扫描那行的 `priority` 保证排最后。只有扫描默认开着，
  两条攻击链路默认关着，理由和 `evo_bot.AUTO_ENABLED` 默认 False 一样。

- 配置：`scheduler_config` 新增 `restart_cooldown_seconds`（默认 300）。
- 数据库：迁移 `c7e4a1b95d62` 给 `scheduler_config` 加上述一列
  （`server_default="300"`，老库里已有一行配置，不给默认值 SQLite 会拒绝这条 ALTER）。
  另新增 `mission_tasks` / `scheduler_config` 的种子行写入（应用层，不在迁移里）。
- 验证：`pytest`（844 passed，较基线 +62）、`ruff check`、`mypy`（90 源文件零问题）。
  四处关键实现各做了一次变异验证——把实现改坏、确认对应测试真的变红、再还原：
  连续失败自停（红 2）、重启冷却（红 6）、配额的 UTC 午夜（红 3）、
  `pending_reports_for_kind` 的 `grace`/`max_age` 接线（互换红 3、忽略配置红 1）。
- 安全：**任何时刻最多一个子进程**——`MissionSupervisor` 拒绝并发 `start()`，
  `MissionScheduler` 加可重入锁（tick 在后台线程、页面操作在请求线程，没有锁的话
  一次「结束」可能落在 tick 的「起进程」中间，控制台以为停了、实际还有一个在点）。
  权威航线闸门仍在 runner 的 `LineCapacityGate`（它看屏），调度器的 `count_inflight()`
  只是乐观估算，估高了最坏是 runner 空跑一轮就退。`LiveDriver(allow_actions=)`
  的开关位置不变，拟人化点击路径不动，`pyautogui.FAILSAFE` 不关。
  **测试中从不真的 `Popen` 一个 runner**，`launch` 一律注入假的。
- 回滚：`git revert` 这一串提交；迁移 `c7e4a1b95d62` 有 downgrade。
