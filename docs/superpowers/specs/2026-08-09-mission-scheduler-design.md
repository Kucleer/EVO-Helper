# 控制台任务调度器 设计规格

日期：2026-08-09
需求来源：交接文档《TODO-交接.md》需求 4「控制台整合」，经本轮澄清后大幅扩写。

---

## 一、范围

**做**：把网页控制台从「建计划 + 定时窗口」改成「三条任务链路 + 优先级调度 + 开始/结束」。

**做（用户 2026-08-09 追加）**：改造 `tools/scan_console.py` 那个桌面悬浮窗，
让它显示**当前任务**状态并保留快捷键当临时开关。见第十一节。

**不做**：
- 不碰 `scan_plans` / `scan_ranges` / `run_instances` / `time_window_start` / `time_window_end`。
  这些是各 runner 自己的账，继续归 runner 管。交接文档里那份「window 列引用面很宽、
  先别删」的清单**整条作废**——不是推迟删除，是不再需要删。
- 不合并三个 runner 的业务循环。它们各自的实机验证成果保持不动。

## 二、既有事实（都已核实，附代码位置）

| 事实 | 位置 |
|---|---|
| 扫描**不派遣**，全程只读 | `tools/scan_coordinates.py:10,48` |
| 航线闸门已存在（在飞 + 游戏自报空位 + 保留航线三者取交） | `game/capacity.py` `LineCapacityGate` |
| 攻击意图与派遣已入库，含 `target_kind`、`expected_report_at_utc` | `storage/models.py` `attack_intents` / `attack_dispatches` |
| `pirate_loop` 写这两张表 | `tools/pirate_loop.py:510,527` |
| **`BotLoop` 是 `PirateLoop` 的子类**，写库走继承来的 `_record_intent`，其中 `target_kind=TARGET_KIND_PIRATE` **硬编码** → bot 的攻击会被错标成海盗，污染日配额计数 | `tools/bot_loop.py:69`、`tools/pirate_loop.py:501-522` |
| ~~**`expected_report_at_utc` 从未被写入**（实测库中 4 条派遣全为 NULL）。原因不是「读到了没传出来」，而是**从来没人去读**~~ **已修复**（`attack()` 与 `scout()` 现在都读 `BRIEFING_FLIGHT_ROI`）。保留这条是因为它是后续所有改动的因由；`BRIEFING_ARRIVAL_ROI` 至今仍是零引用 | `tools/pirate_loop.py`、`game/pirate_ui.py:71-73` |
| 「该等还是该收」已有纯函数判据，且已定义 NULL = 立即收取 | `domain/report_wait.py` `ReportWaitPlanner` |
| 两个 runner 的 `run()` **已经是单趟就退出**（各遍历一遍输入列表就 return），因此**不需要 `--once`** | `tools/pirate_loop.py:541`、`tools/bot_loop.py:153` |
| **`bot_loop` 每个目标在进程内 `time.sleep(600)` 等战报**，期间独占鼠标 | `tools/bot_loop.py:59,176` |
| 海盗的进程内等待只有 45 秒，不构成问题 | `tools/pirate_loop.py:81` |
| 主星 `Coordinate(2,137,18)` 硬编码了两遍 | `tools/pirate_loop.py:69`、`tools/scan_coordinates.py:49` |
| 系号上限常量 | `domain/scan_priority.py` `SYSTEMS_PER_GALAXY` |
| web 服务单进程单 worker（`uvicorn.run` 收的是 app 对象） | `web/runtime.py:main` |
| 供 supervisor 测试抄的 `FakeProcess` + 可注入 `Clock` | 原在 `tests/unit/tools/test_scan_console.py`，第十一节改造后已随 `ScanSupervisor` 一并删除；现存于 `tests/unit/application/test_mission_supervisor.py` |

## 三、调度模型

点一次「开始」，调度器常驻运行，直到点「结束」。

### 三条任务链路

| # | 任务 | 底层 runner | 占航线 |
|---|---|---|---|
| 1 | 侦查海盗 + 攻击海盗 | `tools.pirate_loop --scout --attack` | 是 |
| 2 | 攻击侦查 bot + 攻击 bot | `tools.bot_loop --probe --attack` | 是 |
| 3 | 扫描全星系 bot | `tools.scan_coordinates` | **否** |

优先级由用户在页面上**拖拽**排序，与编号无关。复选框单独控制是否参与。

**但扫描恒在最后一位，不可拖动。** 它永远有活干（不派遣、没有完成态），因此排在谁
前面谁就永远轮不到：把它拖到海盗之前，等于当天 32 次配额悄无声息地全部流失。
而「只跑扫描」这个诉求把另外两个复选框取消即可达成，不需要靠排序表达。

这条同时落在两处：`decide()` 的排序键结构性地把 `SCAN` 排在所有非 `SCAN` 之后
（防住数据库里出现坏行），页面上扫描那一行不给拖并标注「始终填空隙」。

### 「有活干」判据

- **海盗**：当日配额未用尽 **且**（估算空闲航线 > 0 **或** 有到期未收的 `PIRATE` 战报）
- **bot**：本轮范围内存在**未完成**的目标 **且**（估算空闲航线 > 0 **或** 有到期未收的 `BOT` 战报）
- **扫描**：恒为真

bot 的每个目标在一轮里走三态，**状态从库里推导，不新增列**——本轮该目标的
`attack_intents` 里，`preset_name` 等于探路预设（`DEFAULT_PRESET.name`）的是探路发，
等于分档预设（AAA / BBB / CCC）的是攻击发：

| 态 | 库里的样子 | 需要什么 |
|---|---|---|
| 待探路 | 本轮无该目标的 intent | 空航线 |
| 待分档攻击 | 有探路 intent 且其战报已到 | 空航线 |
| 待收攻击战报 | 有攻击 intent 但战报未到 | 到期 |
| **完成** | 有攻击 intent 且其战报已到 | —— |

「有到期未收的战报」**不另写判据，复用 `domain/report_wait.py` 的 `ReportWaitPlanner`**：
把该 `target_kind` 下尚无战报、且未被判为「战报缺失」的派遣组成 `PendingReport` 列表，
`plan(...).action is WaitAction.COLLECT` 即为真。

这条复用不是省事，是避免同一判据出现第二份实现。它还顺带带来了正确的
NULL 语义：`expected_report_at_utc` 为空时立即收取（「宁可白跑，也不能无限等一个
不知道何时到的战报」）——而该列**目前恒为 NULL**，见下一节的前置缺口。

用户那条「同时有 2 个攻击任务，前序占满航线时不开下一个」不需要单独实现——它是
「估算空闲航线 > 0」这个判据的自然结果。

### 调度循环

```
tick(now):
    若有子进程在跑:
        若 在跑的是扫描
           且 已驻留 >= MIN_DWELL
           且 存在任一「有活干」的攻击任务:
            抢占（terminate，stopped_by=PREEMPTED）
        否则:
            返回
    # 空闲。排序键是 (是不是扫描, priority)——扫描恒在最后，见上一节。
    按 (kind is SCAN, priority) 升序遍历已启用任务:
        若 该任务有活干(now):
            起它的 runner，返回
    # 都没活干 → 空转，下一 tick 再看
```

**抢占只有一条规则：只有扫描会被打断。** 扫描游标持久化，随时可断。攻击类任务一旦
启动就跑完那一轮，绝不抢占——中途杀掉可能正停在派遣面板上。

**最小驻留** `MIN_DWELL`（默认 60 秒，页面可改）：扫描起来后至少跑这么久才允许被抢占。
否则航线一空一占会引起秒级反复切换，而每次切换都要 `ensure_game_window()` + 认屏。

**战报批量收取**（用户 2026-08-10 追加）：同一分钟内到期的战报归到**一个**读取窗口，
不为每一份单独起一趟。取最早到期的那份，把 `BATCH_WINDOW`（60 秒）内到期的都并进来，
等到这一组里最晚的那份到期（再加余量）才去收，一趟读完。

不这样做的话，10:00:00 和 10:00:30 各一份会变成两趟——而每趟都要
`ensure_game_window()` + 认屏 + 进信箱，且中间还夹一次任务切换。

**分组从最早那份量起，不是链式延展的。** 「`BATCH_WINDOW` 内到期」量的是「距**最早**
那份 60 秒内」，不是「距上一份 60 秒内」。链式的话，每 59 秒来一份就能把收取无限期
推下去——和本规格别处警告过的「防卡死反转成卡死」是同一个形状。从最早那份量起，
等待时间被 `BATCH_WINDOW + margin` 封死。

**批量会让攻击链路的「有活干」短暂为假**（最长约 65 秒），即使它确实有一份战报到期。
这是有意的：那段空隙归扫描，而填空隙正是扫描存在的理由。记在这里，免得日后被当成
「海盗链路有时候明明有战报却在发呆」的故障来查。

**唤醒余量改为 5 秒**（原 1 分钟）。预计时间本来就是本地记的发出时刻加上简报读到的
飞行时长，精度足够；1 分钟的余量是在没有可靠预计时间的年代留的。

**同 kind 的重启冷却** `RESTART_COOLDOWN`（默认 5 分钟）：同一条链路的两次启动之间
至少隔这么久。这条堵的是「立即收取」的空转——`expected_report_at_utc` 为 NULL 时
判据恒为「该去收」，而战报可能只是还没到：runner 进信箱、扑空、退出、下一 tick
判据仍为真、再起一次。不是死循环，但每轮几十秒的导航全是白费，还一直占着鼠标
不让扫描进来。冷却期内该 kind 视为「没活干」，顺位让给下一个。

**冷却只管攻击链路，不管扫描。** 它堵的 churn 是收战报特有的；扫描的游标持久化，
随起随停没有代价。套在扫描上反而制造纯空转：攻击轮两分钟跑完，扫描还要再等三
分钟才准回来——而填这种空隙正是扫描存在的全部理由。
（`MIN_DWELL` 限制多快**离开**扫描，冷却限制多快**回到**某条链路，两者不重复。）

### 空闲航线的估算 —— 一个明确的近似

调度器活在 web 进程里，**它不看屏，因此并不真的知道有几条航线空着**。

估算口径：

```
在飞数   = attack_dispatches 中 accepted=true 且 dry_run=false
           且 line_free_at_utc > now          ← 出发 + 飞行时长 × 2
空闲航线 = usable_limit − 在飞数        (usable_limit = fleet_line_limit − reserved_lines)
```

### ⚠️ 两个钟：战报 1×，航线 2×（用户 2026-08-10 确认）

这是**两个不同的时刻**，用错一个就会白飞一趟舰队：

| 问题 | 时刻 | 用哪一列 |
|---|---|---|
| 什么时候回去收战报？ | 出发 + 飞行时长 **× 1**（战报在抵达时产生） | `expected_report_at_utc` |
| 什么时候能再派？ | 见下面的倍数表 | `line_free_at_utc` |

**倍数按发次类型分岔（用户 2026-08-10 补充）**：

| 发次 | 判据 | 倍数 | 为什么 |
|---|---|---|---|
| 攻击发 | `preset_name` 是分档预设（AAA/BBB/CCC）或海盗预设 | **× 2** | 舰队打完要飞回来 |
| 探路发（攻击侦查） | `preset_name == DEFAULT_PRESET.name`（探路） | **× 1** | **它是单程的**——探路舰队会在攻击中损失，没有返程 |

判据与 `domain/bot_round.phase_of` 用的是同一条（按 `preset_name` 认探路发），
不要另写第二份。

### 侦察发也占航线，但现在完全没记（用户 2026-08-10 确认）

~~`pirate_loop.scout()` 只调 `_launch`，**不写 `attack_intents` / `attack_dispatches`**。~~
**已修复**——但下面这段因果要留着，它是「为什么非得在 `_launch` 之前读飞行时长」的
唯一理由：侦察**占航线且 2× 返航**，海盗一轮最多派 4 发侦察 → 最多 4 条航线对调度器**完全隐形**
→ 它以为航线空着就去派攻击 → 撞上「同时派遣的舰队数量已达上限」。这多半就是那个
弹窗的直接来源。

**补记录时有一个必须避开的陷阱**：配额查询 `count_dispatches_since` 只按
`target_kind` 过滤。侦察若照 `target_kind=PIRATE` 写进去，**每派一发侦察就吃掉一次
攻击配额**——一轮 4 发，32 次额度会以 4 倍速度消失，而且完全静默。

所以要加一列区分派遣性质（`attack_dispatches.mission_kind`：`ATTACK` / `SCOUT`）：

| 用途 | 口径 |
|---|---|
| 日配额计数 | 只数 `mission_kind=ATTACK` 且 `target_kind=PIRATE` |
| 在飞数（航线） | **全都数**，攻击和侦察一样占航线 |

倍数表因此变成三行：

| 发次 | `mission_kind` | 判据 | 倍数 |
|---|---|---|---|
| 海盗攻击 / bot 分档攻击 | `ATTACK` | `preset_name` 非探路 | × 2 |
| bot 探路（攻击侦查） | `ATTACK` | `preset_name` 是探路 | **× 1**（单程，会损失） |
| 侦察探测器 | `SCOUT` | —— | × 2（会飞回来） |

**侦察简报页上有飞行时间**（用户 2026-08-10 确认）。所以 `scout()` 必须和 `attack()`
一样，在点「出发！」**之前**读一次 `BRIEFING_FLIGHT_ROI`——那是同一块简报面板，
只是「任务类型」显示为侦察。

不读的话，侦察发的 `line_free_at_utc` 恒为 NULL，按既定的 NULL 语义**仍然不计入
在飞数**——记了账等于没记，「4 条侦察航线对调度器隐形」这个原始症状原封不动。
配额、战报、bot 三态那三个陷阱是避开了，但症状没治。

ROI 假定与攻击简报相同。读不出来时照既有语义写 NULL，等于退回改动前的行为，
不会更糟。

**曾经的错误**：`count_inflight()` 用 `expected_report_at_utc > now` 数在飞数，
也就是按 1× 判定航线已空。它的文档字符串写着「这边问的是**舰队回来没有**」——
意图是对的，用的那一列却回答的是「战报出来没有」。后果是调度器在航线其实还占着
的时候就去派，撞上游戏的「同时派遣的舰队数量已达上限」，白跑一轮。

`line_free_at_utc` 与 `expected_report_at_utc` 一样在派出时算好存下（读不到飞行
时长时同样留 NULL，NULL 不计入在飞数——宁可估高，估高有闸门兜底，估低则是航线
空着不派、没人兜）。

**攻击侦查那一批只看 1×**：探路发要等的是战报（读出来才能分档），不是航线返航。

这个估算**不包含用户自己派出去的舰队**，因此是乐观的。`reserved_lines` 正是为这段
误差保留的缓冲。

**权威闸门不变，仍在 runner 里**：`LineCapacityGate` 在每次派遣前看屏复核。调度器的
估算只用来决定「值不值得起一个进程」；估高了，最坏结果是 runner 起来发现没位子、
空跑一轮就退，不会误派。这个分工必须保持——不要试图把看屏搬进调度器。

### 日配额

游戏硬限制：海盗每天 32 次攻击，超限后收到邮件通知且**攻击被强制返回**。
重置点 **UTC 00:00**（即本地 UTC+8 的每天早上 8 点）。

**计数口径：数当日海盗派遣数**（`attack_dispatches ⋈ attack_intents`，
`target_kind=PIRATE`，`dispatched_at_utc >= quota_day_start_utc(now)`）。

一度考虑改数**战报**，理由是能算上用户手动打的那几发。**已放弃**——用户口径
（2026-08-10）：开启海盗任务的当天完全不手打，开任务之前也不打。手动那条路
既然不存在，战报计数就只剩缺点：战报是抵达之后才产生的，连派 5 发、一份都没回时
计数仍是 0，反而会继续派。派遣数在派出的那一刻就记上，正是配额需要的时机。

宁可少打一次，也不白飞一趟舰队。

**不再需要认那封超限邮件。** 第二道保险是游戏自己的禁止（用户口径：超了会发邮件
并强制返回）。原先设想的「runner 认出超限通知就写 `quota_exhausted_until_utc`」
需要一个谁都没见过的画面的 ROI，现在整条取消——`quota_exhausted_until_utc` 这一列
保留（手工封锁仍可用），但不再有自动写入方，也不再是上线前提。

### 完成态

- **海盗**：当日配额用尽 → 退出调度，到 UTC 00:00 自动复活。
- **bot**：本轮范围内每个目标都收到**攻击发**（非探路预设）的战报 → 任务完成并退出，
  只剩扫描。**不自动开新一轮**；页面提供显式的「重开一轮」按钮
  （把 `round_started_at_utc` 推到当前）。分档判定为「不值得打」而没派攻击的目标，
  同样计入完成——它已经走完该走的流程。
- **扫描**：无完成态。

**防卡死**要两条规则，缺一条就会反转成永久卡死：

1. 有 `expected_report_at_utc` 的：过了它再加宽限期 `REPORT_GRACE`（默认 30 分钟）
   仍读不到战报 → 判缺失。
2. **`expected_report_at_utc` 为 NULL 的：按 `dispatched_at_utc` 算**，超过
   `MAX_REPORT_AGE`（默认 6 小时）仍未闭合 → 同样判缺失。

第 2 条不能省。`ReportWaitPlanner` 见到任何一条 NULL 就无条件返回 `COLLECT`，
而库里**现存的派遣全部是 NULL**（飞行时间从来没人读过，历史也不回填）。
只写第 1 条的话，NULL 的派遣既永远「可收」、又永远不被判缺失——海盗的
「有活干」右半边被钉死为真，调度器每个 tick 都去收一封永远不会到的战报，
扫描永远抢不到空隙。**防卡死机制会原样变成卡死机制。**

判为缺失的目标写一条 `target_revisits`（`scope=BOT_REPORT_MISSING`）并**跳过**，
不计入未完成集合，也**必须从 `pending_reports_for_kind` 的结果里排除**——
否则它照样把 `COLLECT` 钉死。

## 四、数据模型

三张新表，一条迁移。

### `mission_tasks`（三行，`kind` 唯一）

| 列 | 说明 |
|---|---|
| `kind` | `PIRATE` / `BOT` / `SCAN` |
| `enabled` | 复选框 |
| `priority` | 拖拽出来的次序，升序即优先级 |
| `params_json` | 海盗：`{"radius": 10}`；bot：`{"galaxy":2,"first_system":100,"last_system":200}`；扫描：`{}` |
| `round_started_at_utc` | 仅 bot 用：本轮从何时算起，把上一轮的战报排除在完成判据外 |
| `quota_exhausted_until_utc` | 仅海盗用：硬信号写入的封锁截止时刻，可空 |
| `consecutive_failures` | 连续异常退出计数 |
| `disabled_reason` | 自动停用的原因，可空 |

### `mission_runs`

`kind` / `command` / `pid` / `started_at_utc` / `ended_at_utc` / `exit_code` /
`stopped_by`（`USER` / `SELF` / `PREEMPTED` / `SHUTDOWN` / `UNKNOWN`）/ `log_path`。

### `scheduler_config`（单行）

`fleet_line_limit` / `reserved_lines` / `pirate_daily_quota`（默认 32）/
`min_dwell_seconds`（默认 60）/ `report_grace_minutes`（默认 30）/
`restart_cooldown_seconds`（默认 300）。

航线是全局资源，不属于任何单个任务，所以不放在 `mission_tasks` 里。

### 不新增表的三件事

- 日配额：查 `attack_dispatches ⋈ attack_intents`。
- bot 完成判据：查 `battle_reports ⋈ attack_dispatches ⋈ attack_intents`，
  取 `intent.target` 落在范围内、且战报晚于 `round_started_at_utc` 的目标集合。
- 战报缺失被跳过的目标：写已有的 `target_revisits`（其语义正是「需要复查的目标」）。
- 分档判为「不值得打」的目标：同样写 `target_revisits`，用另一个 scope
  （`BOT_TIER_NEGLIGIBLE`）。**不要写 `attack_intents.guard_status`**——那一列已经
  被 `application/workflow.py` 用 `ALLOWED` / `REFUSED` 占着，`logs.html` 会把它
  渲染成「未派出 · {原因}」。往里塞第三套词汇，日志页会给出错误的未派出原因。

**在飞数**（航线估算用，**跨 kind 的全局量**——航线是全局资源）：
口径见第三节「空闲航线的估算」与「两个钟」，**以那里为准**，此处不再复述。

⚠️ 这段原本写着「`expected_report_at_utc > now` 且尚无 `battle_report`」——
那正是第三节点名的**那个错误本身**（按 1× 释放航线、并且拿战报当侧门）。
一份看起来权威、照着做却会撞上「同时派遣的舰队数量已达上限」的说明，
比没有说明更糟。同一条口径不要在两处各写一份。

### 调度器开关不持久化

控制台重启后一律停在「已停止」，页面提示「上次是运行中」。重启多半意味着出了事，
自动接着派舰队不是好默认。

## 五、runner 契约与进程生命周期

### 单趟即退，不在进程内等战报

两个 runner 的 `run()` 已经是单趟就退出，**不需要 `--once`**。

但 `bot_loop` 现在每个目标 `time.sleep(600)` 等战报，期间独占鼠标——5 个目标就是
50 分钟，扫描一次也插不进去。这与「等待攻击路线时进行扫描」的需求直接冲突。

**改为「派出即退出」**：拆掉 `REPORT_WAIT_S` 的干睡，一趟只推进每个目标一态
（派探路 / 读战报后分档攻击），把 `expected_report_at_utc` 写进库然后退出。
调度器拿这段时间去跑扫描，到点再把 bot 起来。这正是 `domain/report_wait.py` 开头
写下的设计意图：「派出之后助手**不持有会话**……到点再回来登录收报告」。

海盗的 `SCOUT_REPORT_WAIT_S = 45` 秒不动——45 秒不值得为它拆流程。

`scan_coordinates` 不需要改，它本来就可中断，调度器直接 `terminate`。

**退出码是唯一的进程间协议**：0 = 这一轮正常跑完，非 0 = 异常。调度器不解析 stdout，
所有判据一律查库——这样调度器看到的和 `/logs` 页面看到的是同一份事实。

### 生命周期

- **起**：组命令 → `Popen(stdout=按种类分的日志文件, stderr=STDOUT)` → 写 `mission_runs`。
  `--probe --attack` / `--scout --attack` 这些开关由命令行带过去；
  `LiveDriver(allow_actions=)` 的判断**仍留在各 runner 的 `main()` 里**，控制台不复制这份逻辑。
- **停**：`terminate()` + `wait(5)`，立刻杀。写回 `ended_at` / `exit_code` / `stopped_by`。
- **自退**：app 启动时挂 asyncio 后台任务，每秒 `poll()` 收退出码。不能只在页面轮询时收——
  没人开着页面时那条记录会一直挂在「运行中」。
- **不自动重试**：失败多半是「窗口抢不到前台」或「甩鼠标触发 FAILSAFE」，重启只会再来一遍。
- **连续失败自停**：同一任务连续异常退出 3 次 → 写 `disabled_reason` 自动停用并在页面标红。
  没有这条，调度循环会在一个坏掉的任务上变成满速空转的重启循环。
- **孤儿进程**：
  1. FastAPI lifespan 关闭时主动 `terminate()` 子进程，覆盖正常重启。
  2. 表里存 `pid`；启动时发现 `ended_at` 为空的行 → 标 `stopped_by=UNKNOWN`，页面顶部亮红条 +
     「强制结束」按钮。**不按 pid 自动开枪**——pid 会被系统回收复用。

     ⚠️ **顺序要紧**：pid 必须在**标记之前**读出来。标记会把那些行闭合，闭完就再也
     认不出哪条是孤儿，红条上的 pid 只会是空的。

     「强制结束」按钮实际做三件事：停掉自己手上的子进程（走 `stop()`——只杀不停的话
     下一 tick 立刻又起一个，按钮看着毫无作用）、兜底闭合、**清掉内存里的红条状态**。
     第三件没有的话红条永远不消失。全程不按 pid 开枪。

## 六、页面

`missions.html` 重做为「调度台」：

```
调度器  ● 运行中 0:41:12                    [ 结束 ]
当前    海盗侦查攻击 · 已运行 0:02:07 · var/logs/mission-pirate.log

⠿ ☑  侦查+攻击海盗    半径 10      今日 12/32 · 运行中
⠿ ☑  扫描+攻击 bot    2:100–2:200  等航线 · 还剩 37 个未完成
   ☑  扫描全星系 bot   —            待命   （无拖拽把手，标注「始终填空隙」）
```

- 拖拽改优先级，复选框控制参与，参数就地编辑。
- 状态列取值（八档）：`运行中` / `等航线` / `冷却中` / `待命` / `未启用` /
  `配额用尽` / `已完成` / `已停用`。

  **状态与随行说明是两格，不是一个拼出来的字符串。** 「次日 08:00 恢复」「停用原因」
  这类文字在 `detail` 字段里，页面渲染 `status` 一格 + `detail` 一格。写成
  `配额用尽（次日 08:00 恢复）` 会诱导实施者去拼字符串。
  bot 显示 `已完成` 时同行给出「重开一轮」按钮。

  `未启用` 与 `冷却中` 这两档不能拿别的顶替：复选框没勾的任务显示「待命」是**谎话**
  （它永远不会被起起来）；处在重启冷却里的链路显示「等航线」会让用户去调航线数，
  调完还是不动。
- bot 的系号区间旁**实时回显「该范围内已记录 bot：N 个」**；N=0 时禁止启用该任务。
- 海盗半径旁回显实际覆盖区间（如「2:127 – 2:147，21 个系」）。
- 下方接 `mission_runs` 历史。
- **撤掉**「新建扫描任务」表单与「时间窗口 UTC+8」chip。`/api/plans` 接口保留不动。

### API

`GET /api/scheduler`、`POST /api/scheduler/start`、`POST /api/scheduler/stop`、
`PATCH /api/missions/{kind}`（开关 / 参数 / 优先级，kind 大小写不敏感）、
`POST /api/missions/BOT/new-round`、
`POST /api/scheduler/force-kill`（孤儿红条用——它动的是调度器不是某一行任务，
所以挂在 scheduler 命名空间下）。

`GET /api/scheduler` 的形状（两个消费者共用：页面与桌面悬浮窗）：

```
{running, started_at_utc, current: {kind, label, started_at_utc, log_path} | null,
 orphan_pid,
 tasks: [{kind, label, enabled, priority, params, status, detail, summary, disabled_reason}]}
```

`tasks` 已按 `scheduling_order` 排好（扫描恒最后）；`label` 是链路中文名，
服务端下发，悬浮窗不用自己拼；`summary` 是参数回显；`detail` 是随行事实
（「今日 12/32」「还剩 37 个未完成」）。

**`PATCH /api/missions/SCAN` 带 `priority` 或非空 `params` 一律 400。**
领域层已经结构性地把扫描钉在最后，所以「忽略」和「拒绝」行为上等价——
正因等价才必须拒绝：默默收下一个不起作用的写入，页面会显示「排序已保存」、
刷新后弹回原位，用户只能得出「拖拽坏了」的结论。

**参数校验只在动了参数、或这一下在启用时做。** 只改优先级、或要关掉任务时不校验，
否则参数填错一次连关都关不掉。

**调度器自己的运行时长**同样不持久化（重启即无），记在 `MissionScheduler` 上。
连点两下「开始」不会把秒表按回零。

## 七、参数换算

`domain/missions.py`，纯函数，不碰 IO：

- `pirate_systems(origin, radius)`：按 `(abs(s − origin.system), s)` 排序 →
  `137, 136, 138, 135, 139 …`，等距时小的在前。越界系号**钳制**到
  `[1, SYSTEMS_PER_GALAXY]` 而非报错——半径填大了应当是「到边为止」。
- `bot_targets_in_range(targets, galaxy, first, last)`：筛系号区间，保持坐标序。
- `scan_command()` / `pirate_command(systems)` / `bot_command(targets)`：组命令行。
  **拆成三个而不是一个 `mission_command(kind, params)`**——三条链路的参数类型
  本来就不通，合成一个入口就得让 `params` 退化成 `dict[str, Any]`，在 strict mypy
  下等于放弃检查。下游（`MissionSupervisor`）按种类调对应的那个。

`ORIGIN` 应当只有一份。**当前状态：仍是三份**（`domain/missions.py`、
`tools/pirate_loop.py:69`、`tools/scan_coordinates.py:49`），因为波次 1 的并行拆分
不允许 D 去改 runner 文件。收敛留到波次 1 合流后处理——见第十节的收尾。

**校验**（不合格拒绝启用，不起进程）：半径 ≤ 0；系号区间首尾颠倒；范围内一个已记录
bot 都没有；命令行长度逼近 Windows `CreateProcess` 的 32767 上限时报错而非截断。

## 八、安全不变量

以下每一条都不得因本次改动而松动：

1. 任何时刻**最多一个子进程**在点鼠标。一个游戏窗口，一个鼠标。
2. 权威航线闸门留在 runner 的 `LineCapacityGate`，调度器的估算无权代替它。
3. `LiveDriver(allow_actions=)` 的开关位置不变（各 runner 的 `main()`）。
4. 拟人化点击路径不动；`pyautogui.FAILSAFE` 不关。
5. `expedition_reports.py` 仍是只读。
6. 测试中**绝不真的 `Popen` 一个 runner**——那会在 CI 上去点真实鼠标。`launch` 一律注入假的。

## 九、测试策略

| 层 | 测什么 |
|---|---|
| 单元 · `domain/scheduler.py` | **最厚的一层**。纯函数：输入（任务列表、优先级、当日配额已用、估算空闲航线、最近到期战报、时钟、当前在跑的是谁）→ 输出（起谁 / 抢占谁 / 什么都不做）。表驱动覆盖四个场景：勾 123 的完整流转、勾 13 的间歇填充、两个攻击任务在航线占满时的让位、配额用尽后只剩扫描 |
| 单元 · `domain/missions.py` | 排序确定性、越界钳制、区间筛选、命令组装、四条校验的拒绝 |
| 单元 · `MissionSupervisor` | `FakeProcess` + 假 `Clock`：抢占、最小驻留、连续失败自停、孤儿标记 |
| 集成 · storage | 三张新表持久化；日配额查询；bot 完成判据查询；战报缺失跳过 |
| e2e · web | 调度台渲染、start/stop、拖拽排序接口、N=0 拒绝启用 |

基线不许退化：`python -m pytest tests -q && python -m ruff check src tests && python -m mypy src`。

## 十、交付分段与并行拆分

### 第一段：修正攻击记录的两个缺陷

调度器的两条核心判据（日配额、bot 完成度）都建立在 `attack_intents` /
`attack_dispatches` 之上，而这两张表现在记错了、也记漏了：

1. **`target_kind` 错标**。`BotLoop` 继承 `PirateLoop._record_intent`，那里
   `target_kind=TARGET_KIND_PIRATE` 硬编码。改成可由子类覆盖的类属性，
   `BotLoop` 覆盖为 `TARGET_KIND_BOT`。不修这条，bot 每打一发都会占掉一格
   海盗的当日配额。
2. **`expected_report_at_utc` 恒为 NULL**，因为**从来没人去读飞行时间**。
   新增一个只读的 `_read_briefing()`，在点「出发！」**之前**读
   `BRIEFING_FLIGHT_ROI`（出发后那一屏就没了），经 `parse_game_duration` 解析，
   再由 `_record_dispatch()` 调 `record_flight_time` 写入。

   **`_launch()` 的返回类型不动，仍是 `bool`**：让它返回简报会把「闸门拒绝、
   根本没派出去」和「派出去了但飞行时间读不出来」混成同一个 `None`，
   而这两者的处置完全相反。同理，**读不到飞行时间不阻止派遣**——它是闹钟
   不是闸门，为一次 OCR 抖动毙掉一发健康的攻击是本末倒置。写 NULL，
   靠既定的「未知即立即收取」降级。

3. **`bot_loop` 拆掉进程内干睡**，改为「派出即退出」，一趟只推进每个目标一态。
   见上一节。

**不做历史数据回填**：库里现有 14 条 intent 全标着 `pirate`，而无法从坐标可靠地
反推哪些其实是 bot（bot 与海盗都可能落在同一批坐标上）。样本只有 4 条派遣，
凭空改标签比留着更糟。计划里改为**核对**：修完之后跑一次统计，确认新记录的
标签分得开。

独立价值：做完 `/logs` 上 bot 与海盗分得开，战报等待时间第一次真正可用，
且 bot 链路不再一睡 50 分钟。

### 第二段：调度核心

`domain/missions.py` + `domain/scheduler.py` + 三张表与迁移 + 仓储查询 +
`MissionSupervisor`。命令行先跑通。（runner 侧的改动全在第一段的 B 里。）

### 第三段：页面

调度台 + 拖拽排序 + 历史 + 上面那组 API。

### 可并行的工作单元

依赖只有三条：`E` 要 `A`+`C`；`F` 要 `C`+`E`；`G` 要 `F`。其余互不相干。

| 波次 | 单元 | 触碰的文件 | 依赖 |
|---|---|---|---|
| 1 | **A** `domain/scheduler.py` 纯函数 + 测试 | 新文件 | 无 |
| 1 | **B** `target_kind` 可覆盖 + 简报写入 dispatch + `bot_loop` 改「派出即退出」 | `tools/pirate_loop.py`、`tools/bot_loop.py` | 无 |
| 1 | **C** 三张表 + 迁移 + 仓储查询（配额 / 完成判据 / 在飞数） | `storage/models.py`、`storage/repository.py`、`alembic/` | 无 |
| 1 | **D** `domain/missions.py` 参数换算 + 测试 | 新文件 | 无 |
| 2 | **E** `MissionSupervisor` | `application/` 新文件 | A、C |
| 3 | **F** API | `web/app.py`、`web/schemas.py`、`web/persistent_service.py` | C、E |
| 3 | **G** 页面 | `web/templates/missions.html` | F |

波次 1 的四个单元文件不重叠，可同时开工。两个 runner 的改动**全部集中在 B**，
避免两个 agent 同时改 `pirate_loop.py`。

### 波次 1 合流后的收尾（这些是并行拆分欠下的债，不做就一直欠着）

1. **收敛 `ORIGIN`**。把 `tools/pirate_loop.py:69` 与 `tools/scan_coordinates.py:49`
   两处改成从 `domain.missions` 导入。并行时不能碰这两个文件，所以现在是三份——
   主星改一次要改三处，正是本该消掉的问题。
2. **删掉 `repository: Any` 的临时标注**。`tools/bot_loop.py` 里为了在 Task 7
   落地前过 mypy strict 而放宽的两处，合流后应还原成正常类型。
3. **`bot_dispatch_facts` 的返回类型**从 `list[Any]` 改成 `list[DispatchFact]`。
   `DispatchFact` 是纯 domain dataclass，不构成 import 环，模块级 import 即可。
4. **`pending_reports_for_kind` 补时间下界并排除已判缺失的派遣**（见第三节防卡死）。
   现状会让「有到期未收的战报」永久为真。
5. **补 `count_inflight()`**（跨 kind 的在飞数），波次 1 漏做了。
6. **`bot_dispatch_facts` 补 `accepted` / `dry_run` 过滤**。兄弟方法都过滤了，
   这个漏了：一条被游戏拒掉的派遣会被当成「已派出且永远收不到战报」，
   该目标就永远停在待收状态，bot 的完成态永远达不到。
7. **`mark_bot_target_skipped` 只改最新一条**，`since` 改成必填。现在它改所有匹配
   行，且 `since=None` 分支会把该坐标历史上每一轮的每一条 intent 全刷成跳过。
8. **给 `bot_dispatch_facts` / `mark_bot_target_skipped` 补集成测试**。规格第九节
   点名的四件事，波次 1 只测了两件。

### 收尾

`.changes/` 补一条变更记录（照 `.changes/template.md`）。

## 十一、桌面悬浮窗改造（用户 2026-08-09 追加）

`tools/scan_console.py` 保留，但**从「进程启动器」降级为「调度器的瘦客户端」**。

### 为什么这不是可选的优化

它**曾经**是全仓唯一真正 spawn runner 的地方（改造前的 `scan_console.py`）。调度器一旦上线，
就会有**两个互不知情的东西在抢同一个鼠标**：调度器以为只有自己在派，而 Alt+F8
还能另起一个扫描进程。规格第八节第 1 条「任何时刻最多一个子进程在点鼠标」是硬
不变量，靠约定守不住，只能靠**取消第二个启动器**。

### 改造后的行为

- **不再自己起进程。** `ScanSupervisor` 与 `launch_scan()` 从本模块移除——那份职责
  已经由 `application/mission_supervisor.MissionSupervisor` 承担。
  （`tests/unit/tools/test_scan_console.py` 里针对 `ScanSupervisor` 的用例随之迁移
  或删除；`FakeProcess` + 可注入 `Clock` 的写法已被 supervisor 的测试继承。）
- **显示当前任务。** 轮询 `GET /api/scheduler`，状态行显示正在跑的是哪条链路
  （侦查+攻击海盗 / 扫描+攻击 bot / 扫描全星系 bot）与已运行时长，而不再只有「扫描中」。
  调度器空闲但在运行时显示「待命」，已停止时显示「已停止」。
- **快捷键控制整个调度器。** Alt+F8 → `POST /api/scheduler/start`，
  Alt+F9 → `POST /api/scheduler/stop`。等同于网页上的开始/结束。
- **右键仍然只停不启。** 这条现有性质保留，理由不变（做成切换的话，在状态刚变过的
  那一瞬右键会变成又起一轮）。它现在停的是整个调度器。
- **连不上服务时显示「未连接」，什么都不做。** 不自己拉起 web 服务，更不退回
  「自己跑扫描」的旧行为——调度器可能其实正在跑，只是一时接不上，那时自己
  再起一个进程正是要防的双主人。快捷键按下去只提示服务未启动。

### 保留不动的

置顶且不抢焦点（抢了焦点，下一次点击就会打在它身上）、无边框、双击退出、左键拖动、
快捷键逐个注册且一个被占不影响另一个（实机上 Alt+F9 被 NVIDIA Overlay 占过）。

### 依赖与次序

依赖第六节那组 API，因此排在波次 3 的 Task 10 之后。

**但必须排在页面之前。** 在 `scan_console` 还能自己 spawn 的这段时间里，
第八节的安全不变量 1（最多一个子进程点鼠标）只靠约定守着。所以波次 3 的次序是
**Task 10（API）→ 第十一节（弹窗改造）→ Task 11（页面）**：不给用户任何能启动
调度器的入口，直到第二个启动器已经被拆掉。

### 种子默认值（实施时定的，记录在此）

`SCAN` 默认**开**（只读、不派遣）；`PIRATE` / `BOT` 默认**关**，与 `evo_bot.AUTO_ENABLED`
默认 False 同一个理由——装好不自动打人。`PIRATE` 参数 `{"radius": 10}`，
`BOT` 的系号区间留空（猜不出来，必须用户填）。优先级 0/1/2，扫描恒最后。

改任务的开关 / 参数 / 优先级时会清掉 `disabled_reason` 与连续失败计数——
否则参数填错一次、改好了也永远起不来。

## 十二、环境陷阱（实施中撞到的，记下来省得下次再撞）

**venv 的 editable 安装指向 `D:\eternal-void\src`。** 在 worktree 里裸跑
`python 某个脚本.py` 导入到的是**主工作树**那份代码，不是你眼前这份。
pytest 靠 `pythonpath = ["src"]` 不受影响，ruff / mypy 吃路径参数也不受影响，
但任何「直接跑一段脚本看看效果」的验证都会悄悄验到另一个 checkout。

**worktree 的基线可能不是你以为的那个分支。** 两个并行单元都遇到过 worktree 从
`main` 而不是工作分支开出来的情况——在那里面调度器整段代码都不存在。
开工第一件事：确认 `git log -1` 是不是你要的基线，不对就 reset 过去并报告。

## 十三、三个派遣弹窗（用户 2026-08-10 补充，**ROI 全部待实机标定**）

派遣链路上有三个游戏弹窗会打断流程。**它们分两类，处理方式完全不同**：

| 弹窗 | 性质 | 处理 |
|---|---|---|
| 未选择任何战舰 | 资源耗尽（舰队全在外面） | **停下整轮**，等航线 |
| 同时派遣的舰队数量已达上限。 | 资源耗尽（航线占满） | **停下整轮**，等航线 |
| 没有可执行的任务。 | **单个目标不可打**（8 小时保护期） | **跳过这个目标**，继续打下一个 |

把第三个也当成「停下整轮」是错的：那样一个被保护的目标就能让整轮空转，
而它后面可能还有一堆能打的。

三个都不是故障，**都不该计入连续失败计数**——否则连撞三次就会把整条链路自动停用。

### 第一类：资源耗尽（停下整轮）

处理方式相同：**认出来 → 干净地结束这一轮 → 把空隙让给扫描 → 到点再回来**。

### 1. 「未选择任何战舰」

出现在选舰面板上，原因是**所有战舰都已经派出去了**，没有可选的。

处理：等舰队返回后再发起攻击。返回时刻 = `line_free_at_utc`（出发 + 飞行时长 × 2）。
这正是上面「两个钟」里的第二个——所以这个弹窗本质上是**在飞估算算高了**的现场证据。

### 2. 「同时派遣的舰队数量已达上限。」

出现在简报页点「出发！」之后，原因是航线占满。

处理：同样等 `line_free_at_utc`，或等用户自己的舰队回来释放航线。等待期间可以切进
扫描——扫描不派遣、不占航线，正是为这种空隙准备的。

### 这两个都要做的事

- **不要重试、不要点别处。** 认出来就关掉弹窗、结束这一轮、正常退出（退出码 0）。
- **把「下次什么时候值得再来」写回库**，让调度器据此安排，而不是靠冷却硬等。

### 第二类：「没有可执行的任务。」——跳过这个目标

一个 bot 被攻击之后有 **8 小时保护期**，期间打不了。

**关键：这一发攻击可能来自别的玩家。** 所以保护期**推不出来，只能撞上了才知道**——
不能靠自己的 `last_attack_at_utc` 去预判。

处理：

- 关掉弹窗，**跳过这个目标，继续这一轮的下一个**。不停轮、不算失败。
- 记 `bot_targets.protected_until_utc`，让后续几轮别再白跑一趟。取值：
  - 若我们自己在 8 小时内打过它（`last_attack_at_utc` 在 8 小时内）→
    `last_attack_at_utc + 8 小时`（精确）
  - 否则 → `now + 8 小时`（保守。真实保护期可能只剩 1 小时，但我们无从得知；
    宁可多跳几轮，也不要反复白飞）
- **本轮完成判据里，被保护的目标算「已走完流程」**——和「分档判定不值得打」同一处理。
  否则一个被别人打过的目标能让 bot 任务永远完不成。
- 目标行不存在时要新建（海盗坐标未必在 `bot_targets` 里）。

### 三个都要做的事

弹窗的 ROI 与关键词**需要一次实机标定**（用户已提供截图，坐标要在 1920×917 的
client 空间重新量）。在标定完成前，这三个画面对 runner 都是不可见的——它会走到
「认不出的画面一律停止」那条既有护栏上，安全但不优雅。
