---
issue: 44
agent: domain-storage
type: Fixed
date: 2026-08-11
---

战报认领把自己那一发侦察也当成了候选，于是四发 AAA 全卡在「待战报」。
（补记：PR #95，提交 `90aba2a`。）

## 一、查实结论：压根没认领，不是认错、也不是只认一发

生产库（拷到临时目录查的，没碰原库）2026-08-11：`battle_reports` 里 2:138:2、
13:06:28、VICTORY，`match_status=AMBIGUOUS`、`dispatch_id=NULL`。那份 VICTORY 就是
12:51:11 那发 AAA 攻击的战报（出发点 2:137:18、目标 2:138:2，比派出晚 15 分钟，与
飞行时长对得上）。候选却有两个：12:45:07 的 SCOUT（探测器）和 12:51:11 的 ATTACK。
两个候选 → `AMBIGUOUS` → `dispatch_id` 留空 → `has_report` 永远为假。当天四发 AAA
**无一例外**都是这个形状（2:137:1、2:137:3、2:136:3、2:138:2）——海盗链路的常态就是
「先侦察、判定值得打、再攻击」，同一个出发点、同一个目标、相隔几分钟，所以这不是偶发。

`mission_kind == ATTACK` 这道过滤此前**只有认领这一侧漏了**：`count_dispatches_since`、
`oldest_open_attack_at`、`pending_reports_for_kind`、`bot_dispatch_facts`、
`bot_report_due_at` 五处早就写着它（#34 加进来的那一条）。补上之后每份战报只剩一个
候选，判据是结构性的（侦察发产生不了攻击战报），不是「时间就近」那种猜。

⚠️ **仍然不猜**：真有两发攻击都对得上时照旧记 `AMBIGUOUS`。改的是「谁有资格当候选」，
不是「多个候选时挑一个」。

## 二、修好判据救不回已经在库里的那些行，所以补一条回头重认的路

`append_report` 只在写入的那一刻认领一次，此后没有任何代码回头看它一眼；而
`has_report_at` 那道去重又保证了它们**永远不会被重新读一遍**——下一趟翻信箱看到同一封，
只说一句「库里已有」然后早停。两者一合，那四发永远出不来。

`rematch_report_at` 拿现在的判据把旧行重算一遍（不重开邮件、不重读像素，一次本地写库），
由开工那一趟撞见「库里已有」时触发；只碰 `dispatch_id` 为空的行，一行派遣都不补。
**实机验证**（生产库副本）：重认前 9 份没认领上，5 份 `AMBIGUOUS`（含那四发 AAA 和
08-11 01:35 那份 2:323:10）全部认上，`MATCHED` 36 → 41；剩下 4 份 2026-08-06～09 的仍是
`UNMATCHED`——它们早过了 `MAX_REPORT_AGE`，认不上才是真话，没有为了好看而认。

## 三、开工改为由库驱动，早停保留但要先问过单子

`due_attack_dispatches()` 先从库里算出「已派出、`expected_report_at_utc` 已过、还没有
战报」的那张单子，带着它进信箱。取舍：**早停不删**（用户明确要的），但它要先问单子——
单子上还有没找到的就接着往下开，单子空了才收工。这同时修掉了早停那个盲点：它假定
「库里已有 ⇒ 往下都读过了」，而报告在库里、却没接到该接的那一发上时这个假定是假的。

单子必须有界，否则早停彻底失效、每趟都要把开封预算烧满（每封约八秒）：超过
`MAX_REPORT_AGE` 的掉出单子，还在飞的不进单子，侦察发/被拒的从不进单子。单子每封重查
一次而不是开工算一次拿着走——刚入库/重认的那一发要当场消失。

## 四、当天状态存库，一行读回

`daily_reconciliations` 原先只有 `observed_reports`（信箱观测数），答不上「今天一共算
打了几发」（要现跑 `count_dispatches_since`）和「还有几发在等战报」（库里压根没有）。
补三列 + `daily_attack_status()` 读回：

- `dispatched_count`：当天库内已被接受的**攻击**派遣数（侦察不数）。
- `attacks_used`：两个下界取大，按 UTC 日**只增不减**。多这一层是为了库被换过/清过的
  那天——`dispatched_count` 会掉下来，而游戏里用掉的额度不会退回去。
- `awaiting_reports`：**瞬时状态、可增可减**。做成只增不减的话，舰队全回来之后那个数
  会永远停在最高水位，回读出来的「还在等」全是假的。

⚠️ 仍然一行 `attack_dispatches` 都不补：库里多一条不存在的派遣，调度器就会以为一条
航线被占着、等一份永远不来的战报。

- Configuration: 无新增环境变量。
- Database: `daily_reconciliations` 加三列（`dispatched_count` / `attacks_used` /
  `awaiting_reports`）；不改其他表。
- Verification: `pytest` 1389 passed、`ruff check src tests`、`ruff format --check src tests`、
  `mypy src` 全绿。八处变异逐条确认变红后还原（候选集不再排掉侦察发 / 单子不排已放弃的
  派遣 / 单子不排还在飞的 / 已用配额改成覆盖而不是取大 / 在等的发数也做成粘住不降 /
  重认去动已经认领上的行 / 撞见「库里已有」就收工不问单子 / 撞见「库里已有」不再重认）。
  其中第 4、5、6 条第一轮是**绿**的——断言写弱了（`observed_reports` 自己就取大，盖住了
  `attacks_used` 那一层；重认那条只查了 `dispatch_id` 没查 `match_status`），三条断言都
  改硬了才变红。
- Safety: 信箱白名单一个字没动；页眉时间仍按 UTC 解析；胜负仍走
  `domain/battle_outcome.py`；重认只写 `dispatch_id`，不新增任何派遣。
- Rollback: 去掉候选集里那条 `mission_kind == ATTACK` 即回到原行为（连同全卡「待战报」）；
  `rematch_report_at` 与三个新列都是纯增量，留着不影响调度。
