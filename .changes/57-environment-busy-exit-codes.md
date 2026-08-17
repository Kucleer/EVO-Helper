---
issue: 57
agent: scheduler
type: Fixed
date: 2026-08-17
---

环境故障不再被当成硬失败：runner 该报 `EXIT_ENVIRONMENT_BUSY`（75）的三类条件
终于真的报 75，而不是抛穿 `main()` 按退出码 1 收场。

75 的语义是「环境暂时不行、会自己好，不算失败」——`MissionExit.failed` 看到它
直接返回 False，一次豁免都不消耗。可它在全库 464 轮里只出现过 2 次，因为本该
报 75 的条件全写成了 1。实机 2026-08-17 凌晨那一阵故障（另一台机器缺中文语言包，
画面读不出来）：26 分钟里三条链路各自撞上，**每一轮都吃掉一次**「多条链路同时倒」
的豁免，一路攒到 `MAX_ENVIRONMENT_EXEMPTIONS` 的 6/6 上限；再往下失败就会算到
各个任务头上，连续够了任务被自动停用。按 75 收场的话一次都不会消耗。

缺口仓库自己早就记在案：`web/schemas.py` 里写着「要恢复默认开，先解决两件事：
CLI 把『抢不到前台』按 75 收场……」。冷却逻辑、豁免逻辑当初就是为它设计的，
唯独 CLI 那一侧从来没真的发出过 75。

## 两种处理，分界线在「有没有尽头」

**第 1 类：抢不到前台 → 无条件 75。** 新增 `game.game_window.ForegroundUnavailable`
（原来是裸 `RuntimeError`，全仓一处都没 catch），各 runner 的 `main()` 统一套上
`tools.scan_coordinates.run_with_foreground_guard`。这一条**什么都不做**：不关窗、
不重开 Chrome、一次点击都不发，纯粹让路等用户不再用别的窗口。用户放开鼠标就好，
所以豁免它没有代价。

**第 2、3 类：`unrecognised screen` / 进不去游戏 → 看关窗重开配额。**
（`tools/ranking_scan.py`、`tools/pirate_loop.py`、`tools/scan_coordinates.py`）
这几条和第 1 类有本质区别：它们走 `SessionKeeper` 的恢复阶梯，**在退出之前已经
关掉游戏窗口、重开过 Chrome 并且失败了**。无条件豁免的后果比现状更糟——调度器
每隔一个冷却再起一轮，又吃一次配额，又什么都不推进；配额那道闸挡得住无限重开
Chrome，却挡不住「每 N 分钟起一轮、每轮失败」，而豁免计数不再增长就**再没有任何
东西会最终把它停下来**。今晚那种故障会从「26 分钟后被 6/6 拦住」变成整夜静默空转。

所以判据用重启配额本身——它就是「这是不是暂时的」的现成度量，而且**有尽头**：

    SessionKeeper 还有重开配额  → 75（还有救，值得再试，不消耗豁免）
    配额已耗尽且仍然失败        → 1  （这不是暂时的，让豁免照常攒、最终该停就停）

配额是 3 次 / 滚动 1 小时，所以同一小时里最多三轮能按 75 收场，之后
`restart_and_reenter` 直接被拒、配额恒为 0、退回 1。

`ReconnectOutcome` 因此多带一个 `restarts_left`，默认 **0**——默认值倒向
「按硬失败收场」那一侧是有意的，判错成 75 的代价大得多。

## 顺带

- **`ranking_scan` 补上了恢复阶梯**（关浮层 → 关窗重开），和另外两条链路一致。
  它原先只巡检一次，读到 `UNKNOWN` 就当场返回 1。这不只是少治了 `UNKNOWN` 最常见
  的成因（浮层压着导航条），更是退出码判据的**前提**：只巡检一次的话每一轮都在
  配额满格的状态下报 75，那条判据就没有尽头了。
- **非 SELF 停止不再采信 `terminate()` 留下的退出码**，一律记 `None`。
  Windows 上那个 1 是 `TerminateProcess(handle, 1)` 的内核参数，不是 runner 的
  表态；全库 136 行 PREEMPTED/USER 记录因此都长着「失败」的样子（判据本来就挡住了，
  但 `/logs` 和 `mission_runs` 没法读）。

## 明确不做

- 不给 SCAN 下发 `--limit`（要挑 N 需要数据支撑，另开一条）。
- 不放宽 `_exemptions.clear()` 的触发条件（仍只认 `SELF && 0`）。75 恰恰意味着
  环境**不**好，不宜当清零信号。
- **只放行这三类。** 维护类、配置类、以及任何「不确定会不会自己好」的条件一律
  保持 1：`MailboxUnreachable`、`busy_is_permanent`（出发星球配错）、
  `GameWindowError` 的其余成员。判错方向的代价就是上面那个静默死循环。

- Configuration: 无。
- Database: **无迁移**。`mission_runs.exit_code` 本来就可空。存量行不动。
- Verification: 2196 passed（起点 2166）/ ruff check + format `src tests` 全绿 /
  mypy 117 源文件零问题。四组变异各验过一次（改坏 → 转红 → 还原 → 复跑全绿）：
  ① 三类无条件退 75（`exit_code_for_environment_fault` 恒 75）→ 4 条红；
  ①b pirate 那条恒 `recoverable=True` → 2 条红；
  ② 三类退回 1（恒 1，且 `run_with_foreground_guard` 不豁免）→ 6 条红；
  ③ 维护类/配置类也改成 75（`exit_code_for` 的 `failed` / `busy_is_permanent`
  两支）→ 3 条红；
  ④ `exit_code is None` 当成 0（榜单批次那条判据）→ 1 条红。
- Safety: 全程只改退出码与异常类型，**没有新增任何点击、派遣或写库路径**。
  `ranking_scan` 新走的那一级关窗重开是既有的 `SessionKeeper` 出口（只送一个
  `WM_CLOSE`，不在认不出的画面上动手），配额与另外两条链路完全共用。
- Rollback: 纯代码回滚即可，没有数据形状变化。
