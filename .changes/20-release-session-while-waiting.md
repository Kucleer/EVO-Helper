---
issue: 20
agent: root
type: Added
date: 2026-08-07
---

派出舰队之后松手：助手不再长期持有登录会话，到点再回来收战报。

用户会在助手派出舰队后切换登录去玩。因此「登录中断」不是故障，而是**正常流程的一部分**。

- 新增 `domain/report_wait.py`：
  - `parse_game_duration` 解析游戏内倒计时（`42天17时34分58秒`、`8时3分20秒`、`01:53:19`）。
    读成 0 一律返回 `None`——那说明一个数字都没匹配到，当成「已抵达」会让助手去收一份还没产生的战报。
  - `ReportWaitPlanner` 根据已派出的攻击与当前时间给出 `COLLECT` / `WAIT` / `COMPLETE`。
    已到点的先收，剩下的继续等；飞行时间未知时立即尝试收取而**不是无限等待**。
    唤醒时间上留 1 分钟余量——提前登录只是白跑，但每趟都要抢一次会话。
  - `SessionBackoff` 退避重试：30 秒起倍增、封顶 8 分钟、8 次后安全暂停。
    **助手不和用户抢登录**：两个会话互相顶号会陷入死循环，所以拿不到就退让，用户有优先权。
    封顶刻意压在 8 分钟，太长会导致战报在助手醒来前过期。
- 新增两个运行状态：`AWAITING_REPORT`（等待战报，不持有会话）与 `WAITING_SESSION`（等待登录）。
  两者都属于活动状态，**舰队在飞的几个小时里仍可暂停与紧急停止**。
  被暂停的等待恢复后回到 `AWAITING_REPORT` 接着等，而不是从头重新扫描。
  只有经由 `DRAINING` 收完战报才能到 `COMPLETED`。
- 持久化（迁移 `d5a37c1e08b9`）：派遣表记 `flight_seconds` 与 `expected_report_at_utc`，
  运行实例记 `resume_at_utc` 与 `session_attempts`。这是整个机制的关键——助手可以整个退出，
  恢复时只靠数据库就能算出现在该等还是该收。
- 仓储新增 `record_flight_time` / `pending_reports` / `set_resume_at` / `note_session_attempt`。
  `pending_reports` 只统计**真实**派遣：演习模式不会产生战报，算进来运行就永远等不完。

- 配置：无变更
- 数据库：派遣表与运行实例各加两列，迁移 `d5a37c1e08b9`；均可空或有默认值，既有数据行为不变
- 验证：`pytest`（378 passed）、`ruff check src tests`、`ruff format --check src tests`、`mypy src`；
  集成测试**显式关掉引擎再新开**，证明等待状态完全靠数据库恢复
- 安全：只增加等待与退让，不新增任何派遣路径；演习模式仍是默认
- 回滚：撤销迁移 `d5a37c1e08b9`，移除两个运行状态与 `report_wait.py`
