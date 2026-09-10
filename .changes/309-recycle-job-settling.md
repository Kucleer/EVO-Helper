---
issue: 309
agent: vision-game
type: Fixed
date: 2026-09-11
---

**⚠️ 回收作业永远结不掉，攻击被饿死。**

`mark_recycle_job` **全仓没有一个调用点** —— 作业永远停在 `pending`，
调度器每一轮把同一批坐标当待办再派一遍（`5:332:5` 连着四轮），
而**作业先占名额 ⇒ 攻击彻底停摆**。实测 1 小时 19 轮、0 攻击 0 回收。

修法：`_recycle_once` 的**六条出口**都落状态
（`no_debris` / `screen_error` / `origin_mismatch` / `dialog_rejected` /
`launch_failed` / `dispatched`）。运行器手里没有 job_id（`--recycle` 只带坐标），
所以新增 `finish_recycle_job_for_target(target, origin, …)` 按坐标结。

外加：「面板上没有回收按钮」那条支线原来**一张图都不留**，
「真没残骸」和「读不出来」在日志上一模一样。补原分辨率取证裁片。

- Database: 无迁移
- Verification: `pytest -q` 全绿；源码级断言变异验证过会红
- Safety: 只增加落状态，不改判据
- Rollback: revert 本 PR
