# 控制台任务调度器 设计规格

日期：2026-08-09
需求来源：交接文档《TODO-交接.md》需求 4「控制台整合」，经本轮澄清后大幅扩写。

---

## 一、范围

**做**：把网页控制台从「建计划 + 定时窗口」改成「三条任务链路 + 优先级调度 + 开始/结束」。

**不做**：
- 不碰 `tools/scan_console.py`（桌面悬浮窗 + Alt+F8/F9）。它继续独立存在。
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
| **`expected_report_at_utc` 从未被写入**（实测库中 4 条派遣全为 NULL）。简报数据在 `_launch()` 里读到了但没传出来 | `tools/pirate_loop.py:301,527`、`vision/parsers.py:603-618` |
| 「该等还是该收」已有纯函数判据，且已定义 NULL = 立即收取 | `domain/report_wait.py` `ReportWaitPlanner` |
| 两个 runner 的 `run()` **已经是单趟就退出**（各遍历一遍输入列表就 return），因此**不需要 `--once`** | `tools/pirate_loop.py:541`、`tools/bot_loop.py:153` |
| **`bot_loop` 每个目标在进程内 `time.sleep(600)` 等战报**，期间独占鼠标 | `tools/bot_loop.py:59,176` |
| 海盗的进程内等待只有 45 秒，不构成问题 | `tools/pirate_loop.py:81` |
| 主星 `Coordinate(2,137,18)` 硬编码了两遍 | `tools/pirate_loop.py:69`、`tools/scan_coordinates.py:49` |
| 系号上限常量 | `domain/scan_priority.py` `SYSTEMS_PER_GALAXY` |
| web 服务单进程单 worker（`uvicorn.run` 收的是 app 对象） | `web/runtime.py:main` |
| 供 supervisor 测试抄的 `FakeProcess` + 可注入 `Clock` | `tests/unit/tools/test_scan_console.py:19,48` |

## 三、调度模型

点一次「开始」，调度器常驻运行，直到点「结束」。

### 三条任务链路

| # | 任务 | 底层 runner | 占航线 |
|---|---|---|---|
| 1 | 侦查海盗 + 攻击海盗 | `tools.pirate_loop --scout --attack` | 是 |
| 2 | 攻击侦查 bot + 攻击 bot | `tools.bot_loop --probe --attack` | 是 |
| 3 | 扫描全星系 bot | `tools.scan_coordinates` | **否** |

优先级由用户在页面上**拖拽**排序，与编号无关。复选框单独控制是否参与。

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
    # 空闲
    按 priority 升序遍历已启用任务:
        若 该任务有活干(now):
            起它的 runner，返回
    # 都没活干 → 空转，下一 tick 再看
```

**抢占只有一条规则：只有扫描会被打断。** 扫描游标持久化，随时可断。攻击类任务一旦
启动就跑完那一轮，绝不抢占——中途杀掉可能正停在派遣面板上。

**最小驻留** `MIN_DWELL`（默认 60 秒，页面可改）：扫描起来后至少跑这么久才允许被抢占。
否则航线一空一占会引起秒级反复切换，而每次切换都要 `ensure_game_window()` + 认屏。

### 空闲航线的估算 —— 一个明确的近似

调度器活在 web 进程里，**它不看屏，因此并不真的知道有几条航线空着**。

估算口径：

```
在飞数   = attack_dispatches 中 accepted=true
           且 expected_report_at_utc > now
           且 尚无对应 battle_report 的条数
空闲航线 = usable_limit − 在飞数        (usable_limit = fleet_line_limit − reserved_lines)
```

这个估算**不包含用户自己派出去的舰队**，因此是乐观的。`reserved_lines` 正是为这段
误差保留的缓冲。

**权威闸门不变，仍在 runner 里**：`LineCapacityGate` 在每次派遣前看屏复核。调度器的
估算只用来决定「值不值得起一个进程」；估高了，最坏结果是 runner 起来发现没位子、
空跑一轮就退，不会误派。这个分工必须保持——不要试图把看屏搬进调度器。

### 日配额

游戏硬限制：海盗每天 32 次攻击，超限后收到邮件通知且**攻击被强制返回**。
重置点 **UTC 00:00**（即本地 UTC+8 的每天早上 8 点）。

两个判据取先到者：

1. **计数**：`attack_dispatches ⋈ attack_intents` 中 `target_kind=PIRATE`
   且 `dispatched_at_utc >= 当日 UTC 00:00` 的条数 >= `pirate_daily_quota`。
2. **硬信号**：runner 读信箱时若认出超限通知，写
   `mission_tasks.quota_exhausted_until_utc = 次日 UTC 00:00`。调度器见到未来时间即跳过该任务。

宁可少打一次，也不白飞一趟舰队。

### 完成态

- **海盗**：当日配额用尽 → 退出调度，到 UTC 00:00 自动复活。
- **bot**：本轮范围内每个目标都收到**攻击发**（非探路预设）的战报 → 任务完成并退出，
  只剩扫描。**不自动开新一轮**；页面提供显式的「重开一轮」按钮
  （把 `round_started_at_utc` 推到当前）。分档判定为「不值得打」而没派攻击的目标，
  同样计入完成——它已经走完该走的流程。
- **扫描**：无完成态。

**防卡死**：过了 `expected_report_at_utc` 再加宽限期 `REPORT_GRACE`（默认 30 分钟）
仍读不到战报的目标，写一条 `target_revisits`（`scope=BOT_REPORT_MISSING`）并**跳过**，
不计入未完成集合。一份读不出来的战报不得把任务 2 永久卡住。

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
`min_dwell_seconds`（默认 60）/ `report_grace_minutes`（默认 30）。

航线是全局资源，不属于任何单个任务，所以不放在 `mission_tasks` 里。

### 不新增表的三件事

- 日配额：查 `attack_dispatches ⋈ attack_intents`。
- bot 完成判据：查 `battle_reports ⋈ attack_dispatches ⋈ attack_intents`，
  取 `intent.target` 落在范围内、且战报晚于 `round_started_at_utc` 的目标集合。
- 战报缺失被跳过的目标：写已有的 `target_revisits`（其语义正是「需要复查的目标」）。

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

## 六、页面

`missions.html` 重做为「调度台」：

```
调度器  ● 运行中 0:41:12                    [ 结束 ]
当前    海盗侦查攻击 · 已运行 0:02:07 · var/logs/mission-pirate.log

⠿ ☑  侦查+攻击海盗    半径 10      今日 12/32 · 运行中
⠿ ☑  扫描+攻击 bot    2:100–2:200  等航线 · 还剩 37 个未收战报
⠿ ☑  扫描全星系 bot   —            待命
```

- 拖拽改优先级，复选框控制参与，参数就地编辑。
- 状态列取值：`运行中` / `等航线` / `待命` / `配额用尽（次日 08:00 恢复）` /
  `已完成` / `已停用（原因）`。bot 显示 `已完成` 时同行给出「重开一轮」按钮。
- bot 的系号区间旁**实时回显「该范围内已记录 bot：N 个」**；N=0 时禁止启用该任务。
- 海盗半径旁回显实际覆盖区间（如「2:127 – 2:147，21 个系」）。
- 下方接 `mission_runs` 历史。
- **撤掉**「新建扫描任务」表单与「时间窗口 UTC+8」chip。`/api/plans` 接口保留不动。

### API

`GET /api/scheduler`（状态 + 三条任务 + 当前子进程）、
`POST /api/scheduler/start`、`POST /api/scheduler/stop`、
`PATCH /api/missions/{kind}`（开关 / 参数 / 优先级）、
`POST /api/missions/bot/new-round`、
`POST /api/missions/force-kill`（孤儿红条用）。

## 七、参数换算

`domain/missions.py`，纯函数，不碰 IO：

- `pirate_systems(origin, radius)`：按 `(abs(s − origin.system), s)` 排序 →
  `137, 136, 138, 135, 139 …`，等距时小的在前。越界系号**钳制**到
  `[1, SYSTEMS_PER_GALAXY]` 而非报错——半径填大了应当是「到边为止」。
- `bot_targets_in_range(targets, galaxy, first, last)`：筛系号区间，保持坐标序。
- `mission_command(kind, params)`：组命令行。

`ORIGIN` 从两处硬编码抽到此模块当共用常量。

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
2. **`expected_report_at_utc` 恒为 NULL**。简报在 `_launch()` 里已经读到
   （`DispatchBriefing.expected_report_at_utc`），只是没传出来。让 `_launch()`
   返回简报，`_record_dispatch()` 接收并写入 `flight_seconds` 与
   `expected_report_at_utc`；读不到简报时仍写 NULL（保持现有降级语义）。

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

### 收尾

`.changes/` 补一条变更记录（照 `.changes/template.md`）。
