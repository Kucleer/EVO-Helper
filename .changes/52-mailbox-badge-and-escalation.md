---
issue: 52
agent: vision-game
type: Fixed
date: 2026-08-13
---

修好「开工翻信箱」这条路上的三个缺陷，并补上战报补录的命令行入口。

起因是 2026-08-12 那夜：15 发 bot 攻击派于 15:04–15:50 UTC，6 小时死线 21:04–21:50。
窗口内只跑起过三轮 BOT，其中两轮（23:51 / 00:30）把单子上那 10 发、15 发一个不落地
打印出来，**下一行就放弃了它们**，21 份战报全部过期判缺失。

## 1. `MAIL_BADGE_ROI` 被第三位数字挤爆了

`_on_planet_surface()` 唯一的正面凭据。框标定时未读数是 70（两位），后来涨到
160 / 196 / 332。数字**居中于 x≈1165**、每位约 9px，所以每多一位就同时往左往右各长
4.5px——三位数顶出了那个 55px 宽的框，整块读成空，`_enter_mailbox` 于是报
「切不到自己星球地表」，而现场图上游戏好端端停在地表。

改法**不是把左边界往左挪一点**（那只是把同一个 bug 推迟到 1000 封），而是按**部件
本身**重新定框：`(1148, 55, 1206, 92)` = 信封白块右缘 → 面板右描边，也就是数字在
不改变版面的前提下能占的全部空间。同时加了二值化（阈 150）与三档 lanczos 放大。

在 195 张实拍上（22 张地表 + 173 张别的画面）**0 漏 0 误**；旧配置是 8 漏 2 误。

- Configuration: 无
- Database: 无
- Verification: `tests/integration/vision/test_mail_badge_live.py`（实拍，图不进 Git）
- Safety: 负面那一侧一并守住了——放宽到吃进信封白块会把浮层判成地表，
  然后照地表的坐标往浮层上点。
- Rollback: 还原 `MAIL_BADGE_ROI` / `MAIL_BADGE_THRESHOLD` / `MAIL_BADGE_UPSCALES`

## 2 & 3. 单子非空却翻不了信箱：升级重启，仍然不行就判失败

原先 `reconcile_today` 的 `except RuntimeError` 一律吞掉，理由是「和不做对账一样，
不比它更糟」。**那句话只在单子为空时成立**：单子非空时那几发的 6 小时钟正在走。

- 单子为空 → 行为一个字不变（不写记录，下一轮再试）。
- 单子非空 → 走既有的 `SessionKeeper` 关窗重开（配额 3 次 / 滚动 1 小时，与
  `_require_system_view` 共用），然后**再翻一次**（用一份干净的 `DailyTally`）。
- 重开被拒 / 重开之后还是翻不了 → 抛 `MailboxUnreachable`，`run()` 收进
  `Outcome.failed`，**这一轮不跑目标循环**，退出码 `1`。

退出码刻意**不用** `EXIT_ENVIRONMENT_BUSY`：那一档不计入连续失败，准入条件是
「会自己好」，而这里已经关窗重开过一次仍然不行。

- Verification: `tests/unit/tools/test_mailbox_reconciliation.py`
- Safety: 重启走既有配额，不另起一套；单子为空时一次 Chrome 都不关。

## 4. 新增 `python -m evo_helper.tools.backfill_reports`

`backfill_scout_reports` 写死 `wanted=ReportKind.SCOUT`，战报没有入口，所以那 21 份
此前**没有任何工具能取回来**。

```
python -m evo_helper.tools.backfill_reports --kind {pirate|bot} --since YYYY-MM-DD \
    [--max-pages N] [--max-opens N] [--exhaustive]
```

两种模式，判据分开：默认是**对账模式**（撞见库里已有、且单子上没有欠账就收工，
复用 `_stop_after_known`，所以 `--max-opens 60` 是封顶不是指标，能挂在控制台
「开始」按钮上）；`--exhaustive` 是**补录模式**（一直翻到 `--since`，救过期战报，
它们早已掉出 `due_attack_dispatches` 的 6 小时窗口）。

- Safety: 只读——不删邮件、不领奖励、不派舰队；两条链路都以 `attack=False` 构造。
- Verification: `tests/unit/tools/test_backfill_reports.py`

## 5. 飞行时间：多试几套配方，读不出就存图（PR #112 的后续）

解析器收紧成「部分匹配一律失败」之后，读不出来的从错值变成 `None`，而 `None` 按
`UNKNOWN_LINE_HOLD`（90 分钟）占航线。补了四套 lanczos 配方
（`(6,160)/(5,120)/(3,140)/(6,100)`）、把 `_settle` 加到 6 轮、全败时存现场图。
两张实拍（画面上 `8分26秒` / `8分28秒`）原先一套都读不出，现在读得出且读对。

⚠️ **一套 `nearest` 都没加**：同一块像素上它不是读不出，而是**成功地读错**
（`'8分 PEPE'` → `0:08:00`、`'as} 6秒'` → `0:00:06`），而这个函数取第一个解析成功的。

- Verification: `tests/integration/vision/test_briefing_flight_live.py`、
  `tests/unit/tools/test_pirate_loop_dispatch_record.py`
