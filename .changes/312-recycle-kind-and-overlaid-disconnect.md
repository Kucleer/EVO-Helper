---
issue: 312
agent: vision-game
type: Fixed
date: 2026-09-11
---

**⚠️⚠️ 回收派遣被记成 `ATTACK`：回收喂自己。**

`_record_dispatch` 的默认值是 `MISSION_KIND_ATTACK`，漏传**不报错**。
而回收决策扫的正是「`mission_kind=ATTACK` 且 bot 且航线已释放」
⇒ 每发回收一释放就又生成一条回收作业，**自我放大，攻击永远轮不上**。
实机 03:1x 撞到，6 发全记错。加 `MISSION_KIND_RECYCLE` 并显式传，用例守住。

## ⚠️ 外加：让整夜停摆 80 分钟的根因

**掉线那句话被弹窗盖住时渲染在另一个位置。** 01:45 残骸框开着时连接断了，
「连接已断开，正在重新连接…」被画进**那个框里面**（y≈563），
而 `DISCONNECT_TEXT_ROI` 的下边界是 **500** —— 一个字都读不到。
⇒ 会话守护看不见掉线、不关窗重开，每轮死在「回不到星球地表」，
**42/45 轮空转 80 分钟没人升级**。

新增 `DISCONNECT_TEXT_ROI_OVERLAID = (820, 550, 1100, 578)`，
`disconnect_screen()` 两块都读，命中第二块返回 `DEAD_SESSION`。
用例钉死「两块 ROI 不许重叠」——第二块存在的理由就是第一块够不着。

外加残骸框等待改成重试（`RECYCLE_DIALOG_TRIES=4`）而不是写死 sleep。

- Database: 无迁移
- Verification: `pytest -q` 全绿
- Safety: 记错 kind 的存量行不动（见 `docs/回收闭环/实机验证-2026-09-10夜.md` §4）
- Rollback: revert 本 PR
