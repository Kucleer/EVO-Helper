---
issue: 39
agent: web-api
type: Added
date: 2026-08-11
---

「开始」那一刻把三条链路的配置固化成一条记录，运行中一律拒绝修改，页面同步
置灰。用户口径（2026-08-11）：「任务开始后，调度台固化任务数据，记录任务内容。
并且开始后，无法修改任务，只有结束状态才可以修改」。

## 甲、正因：一轮之内两套口径，而且事后查不出来

`MissionScheduler._step()` 每个 tick（1 秒）都重新去库里读一遍 `mission_tasks`，
所以运行中改一个参数会**立刻**生效到下一轮——而上一轮正拿着旧参数在飞。页面上
那句「改参数按回车或点别处即保存」是真的，只是它没说改动会当场进入调度。

事后翻账时分不出来：`mission_runs` 里只有一行命令行，看不出当时页面上填的是哪
一套、什么时候换的。海盗半径从 5 改到 12 之后，那一轮到底按哪个半径跑的，
全仓没有任何一处记着。

## 乙、固化：一次「开始」一条记录

新增 `application/mission_freeze.py`：

- `FrozenTask`（kind / enabled / priority / **原样的** `params_json`）与
  `MissionConfigFreeze`（固化时刻 + 三条链路）。
  只记**用户改得动的那几样**：`disabled_reason`、`consecutive_failures` 是调度器
  自己的状态，记进来的话，一次「连崩三次被自动停用」会让下一条记录看起来像是
  用户改了什么。
  `params_json` 原样存不解析：它就是调度器待会儿要交给 `_command_for` 的那个
  字符串，解析一遍再存，存下来的就成了「我们以为的配置」。
- `freeze_now()` 按 `MissionKind` 的声明顺序排，**不按 priority** ——两条记录逐条
  对比时，同一条链路必须落在同一格；跟着 priority 排的话，用户把 bot 拖到海盗
  前面之后整张表会错位，看起来像三条链路全改了。
- `MissionFreezeLog` 一次「开始」追加一行 JSONL 到 `var/mission-config-freezes.jsonl`。
  **读在内存（构造时载一次）、写才碰磁盘**：页面每 2 秒问一次状态，没有理由每次
  都去读一遍文件。选文件而不是新建数据库表，一是这份东西只被追加、只被按时间
  倒序读，二是 JSONL 用记事本就能打开——出事时用户要能不开控制台就查。
  读坏行跳过、写失败吞掉：账丢一条是遗憾，调度器起不来是事故。

`MissionScheduler.start()` 只在**停 → 开**这一次跃迁上固化（连点两下不记第二条，
同秒表）。查库在锁外、写文件在锁外，锁里只剩几个字段的赋值——`_lock` 上那条
「绝不能护到查库上去」的约束原样成立（实机 2026-08-11 「点结束毫无反应」）。

## 丙、运行中拒绝修改，返回 409 而不是静默忽略

`MissionConsoleService.patch_mission` 进门先过 `_refuse_while_running`：
`PATCH /api/missions/{kind}` 在锁着时返回 **409**，正文是
「调度器运行中，任务配置已固化，不能修改；点「结束」后可改（被自动停用的链路
仍可点「恢复」）」。页面本来就把 `detail` 显示在那条红字里。

三处判断（任务里点名要我自己定的）：

1. **「恢复」开一个窄口子。** 一条链路完全可能在调度器跑着的时候被自动停用
   （连崩三次，多半是「窗口抢不到前台」这类环境原因），而那正是用户最需要恢复
   它的时刻——一刀切禁掉 PATCH，上一个 PR 刚加的「恢复」按钮就废了，用户只剩
   「点结束、恢复、再点开始」，代价是把另外两条正常的链路一起停掉。
   开这个口子**不破坏固化**：自动停用时 `enabled` 本来就还是 True，
   `disabled_reason` 与失败计数是调度器自己的状态、不是用户填的配置，所以这一下
   不动固化记录里的任何一个字段。因此口子只认「这一行确实处在已停用状态」且
   「这次 PATCH 除了 `enabled: true` 之外什么都没带」——带上 params 或 priority
   就不是恢复，是趁着恢复顺手改一笔。
2. **runner 收尾期算「运行中」。** 锁的判据是
   `enabled or supervisor.running is not None`。正常路径上 `stop()` 是同步的
   （`terminate()` 之后 `wait(5)`），返回时 `running` 已经是 None，所以第二个
   条件一毫秒都不会多锁；留着它是因为这个问题的答案不该藏在别的模块的实现细节
   里——哪天收尾改成异步，锁会自己跟着延长，而不是在收尾途中静默放行一次改参数。
3. **「重开一轮」（`POST /api/missions/BOT/new-round`）不在锁里。** 它不写任何一个
   配置字段，只把 `round_started_at_utc` 推到当前，也就是「按同一套配置再跑一遍」，
   固化记录里的每个字段都还是原样。挡掉它的话，用户开新一轮就得先把整台调度器
   停下来。

## 丁、页面：先显形，再解释，最后给入口

- `/api/scheduler` 增 `config_locked` 与 `frozen_config`（本轮那一份，停着时为
  null）。字段是新增的可选项，桌面悬浮窗的 `parse_scheduler` 对多余字段容错，
  契约不破。
- 运行中把参数框、复选框 `disabled`，并把行的 `draggable` 真的改成 `false`
  （`dragstart` 认的就是这个属性，不是只把把手画灰）；提示语换成
  「调度器运行中，任务配置已固化，不可修改；点「结束」后可改。被自动停用的链路
  仍可点「恢复」。」——只置灰不解释，用户只会得出「这页坏了」。
  「恢复」按钮的显隐仍然只看 `status === '已停用'`，**不看锁**。
- 新增「配置固化记录」面板（就是「要不要给页面入口」的答案）：顶部一行由轮询
  实时显示本轮固化的时刻、三条链路的配置、与上一次相比改了什么；下面一张历史表
  （最近 20 次，服务端渲染）；页脚写出 JSONL 的绝对路径，控制台没开也查得到。
  「改了什么」由 `web` 层算（`_describe_changes`），领域/应用层只存事实。
- 锁着时参数框每轮跟库走（平时只填一次）：没人能在禁用的输入框里打字，而
  「结束」之后框里留着输入到一半的值会看起来和已保存的值一模一样。

**未改动用户已配置的任何取值**：这次只加约束与记录，`priority`、`params`、
`enabled` 一个字都没动过。

- Configuration: 无新增环境变量。固化记录默认落在 `var/mission-config-freezes.jsonl`
  （`DEFAULT_FREEZE_LOG`），只由 `create_persistent_app` 注入；`MissionScheduler`
  自建的那个默认只留在内存，所以测试与假服务不会往仓库里落文件。
- Database: 无迁移，无新表。
- Verification: `pytest tests -q`（1330 passed / 51 skipped）、`ruff check src tests`、
  `ruff format --check src tests`、`mypy src`（93 files）。六处变异验证，均确认变红
  后还原：① `_refuse_while_running` 开头直接 `return` → 6 条红（params/priority/
  enabled 三条拒绝 + 夹带修改 + 未停用行 + 子进程未退）；② 恢复条件放宽成
  `is_revive = enabled is True` → `..._may_not_smuggle_in_a_param_change` 与
  `..._enabling_a_chain_that_is_not_disabled_is_still_refused` 变红；③ `start()` 不
  `append` 固化记录 → 5 条固化用例变红；④ 页面不再 `disabled = locked` →
  `test_the_page_disables_every_edit_control_while_running` 变红；⑤ `freeze_now`
  改成按 priority 排 → `test_the_tasks_are_ordered_by_kind_not_by_priority` 与
  `..._records_what_changed_in_between` 变红；⑥ `config_locked` 去掉
  `supervisor.running` 那一半 → `test_a_child_that_is_still_running_keeps_the_
  configuration_locked` 变红。另外拿临时库 + 假 launcher 起了一台调度台，在浏览器
  里实测了「开始 → 控件置灰、直接打接口得 409 → 结束 → 恢复可编辑」整条路径。
- Safety: 未驱动游戏（launcher 一律是假的），未触碰 `var/evo-helper.db`，未改动
  `tools/`、`vision/`、`storage/`。这次的写入只有一个 append-only 的文本文件，
  写失败不影响「开始」。
- Rollback: 删掉 `_refuse_while_running` 的调用即回到「随时可改」；固化记录与页面
  面板是纯增量，留着不影响调度。
