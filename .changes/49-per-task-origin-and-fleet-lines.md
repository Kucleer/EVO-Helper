---
issue: 49
agent: root
type: Changed
date: 2026-08-13
---

任务有了自己的身份（id + 名字）、自己的出发星球和自己的航线数；航线记账从
「全局一份」改成「**按出发星球各一份**」；bot 攻击可以有多个任务。

用户口径（2026-08-13）：

> 之后的任务需要配置一个出发星球（默认主星，也就是第一颗），以及航线数。也就是
> 可能会新增多个同一个类型的任务，比如 2 个 bot 攻击，从主星出发 5 条航线，
> 从 2 号线出发 2 条航线。

追问确认两件事：**航线上限是按星球各一份的**（不是账号共享），**只有 bot 攻击
需要多任务**（海盗、扫描保持单任务）。

⚠️ **这是两个 PR 里的第一个。** 这一个只做「数据模型 + 记账」；「在游戏里切换
出发星球」是第二个。在它落地之前，配一颗不是主星的出发星球会被**当场拒掉**
（见下面的「临时闸门」）。

## 任务的身份从 `kind` 换成 `id`

`mission_tasks.kind` 上那道唯一约束去掉了，任务改由 `id` 认人：

- 接口从 `PATCH /api/missions/{kind}` 变成 `PATCH /api/missions/{task_id}`，
  另加 `POST /api/missions`（新建，只收 `BOT`）、`DELETE /api/missions/{task_id}`、
  `POST /api/missions/{task_id}/new-round`。
- `domain.scheduler` 的每一条判据（冷却、空手而归、等航线、完成态、状态文案、
  「多个一起倒」的环境故障豁免）都按 `task_id` 认人。按 `kind` 认的话，两个 bot
  任务会共用一份冷却与一份「上一轮空手而归」——主星那个刚跑完，2 号星那个就得
  干等五分钟，而它俩占的根本不是同一份航线。
- `mission_runs` 多一列 `task_id`（历史行留 NULL，那些行不参与冷却判据）。
- 页面上的行**由脚本按 `/api/scheduler` 下发的任务列表建**：行数是用户加出来的，
  服务端渲染不出固定几行。

## 航线记账按出发星球分

`repository` 的四个查询全部多一个**必填**的 `origin`：`count_inflight`、
`next_line_free_at`、`last_dispatch_at`、`pending_reports_for_kind`。出发坐标存在
`attack_intents.origin_*` 上（那一列一直都有），所以只是多 join 一次意图表。

判据抽成 `domain.scheduler.free_lines_for(task, inflight_from_origin, reserved_lines)`。
`reserved_lines`（给用户自己留的缓冲）跟着变成**按星球**生效。

**全局 `scheduler_config.fleet_line_limit` 保留**，含义从「账号一共几条」降级成
「任务没填时用几条」：海盗与扫描没有必要各配一份，新建的任务也该有个不至于一发都
派不出去的起点。真正的上限判据一律走任务这一层。

⚠️ **海盗每天 32 次没有跟着改。** 那是游戏对**账号**的硬限制，不是按星球的；
跟着改成按星球等于凭空把配额翻倍，超了会收到超限邮件、舰队被强制返回。
`SchedulerFacts` 因此分成两层：账号级的那几个字段留在顶层，其余按 `task_id` 挂在
`per_task` 上。

## 临时闸门：还切不过去的星球一律拒掉

`domain.missions.check_origin_dispatchable(origin, current)`。助手目前不会在游戏里
切换当前星球，派遣面板上的出发地就是游戏此刻选中的那一颗。

放行一个和实际出发地不符的 `origin`，代价**不是「打不到」，是账本在撒谎**：舰队从
主星飞出去，台账上却写着从 9:250:8 出发，战报认领（出发坐标 + 目标坐标 + 时间就近）
永远配不上那一发，飞行时间与航线钟也全按错的距离算。

做成一个具名函数、只在 `MissionScheduler._command_for` 与 API 校验两处调用：
切换星球落地时删掉的是这一个函数和它的调用点。

## runner 拿到 `--origin`

`pirate_loop` 与 `bot_loop` 都加了 `--origin`，写进 `attack_intents.origin_*` 的
就是它。**调度器一律显式传**——不传会回落到 `EVO_HELPER_ORIGIN`，于是两个任务的账
可能记到同一颗星球上。手工跑命令行时不给仍然回落，行为不变。

## 固化记录

`FrozenTask` 多了 `task_id` / `name` / `origin` / `fleet_lines`；`origin` 存的是
**解析后**的坐标而不是那三列原样——记录要回答「那一轮舰队从哪出发」，存 NULL 的话
改了 `EVO_HELPER_ORIGIN` 之后旧记录会跟着改口。

⚠️ **旧行照样读得出来。** 生产那份 `var/mission-config-freezes.jsonl` 里全是本轮
之前写的行，没有这四个字段；缺的一律回落到「没有 / 跟着全局走」，那正是那些行当时
的真实语义。逐条对比（`_describe_changes`）先按 `task_id` 认人，认不上再按 kind
回落一次——只为读旧记录，而且只认「上一条里这个 kind 恰好只有一个任务」。

- 配置：任务多了「名字 / 出发星球 / 航线数」三项；出发星球留空 = 用全局主星，
  航线数留空 = 用 `scheduler_config.fleet_line_limit`。既有三行的取值一个没动
- 数据库：迁移 `d2c4b8a71f39`。`mission_tasks` 去掉 `kind` 唯一约束、加
  `name` / `origin_galaxy` / `origin_system` / `origin_position` / `fleet_lines`
  并给 `kind` 单独建索引；`mission_runs` 加 `task_id`。既有三行原样带过去，
  BOT 那行按用户要求显式填主星 `2:137:18` 与**当时的**全局 `fleet_line_limit`
  （读库取值，不写死），PIRATE / SCAN 的出发星球留 NULL 以免堵掉换账号那条路。
  `downgrade()` 在同一 kind 真有多行时**拒绝执行**并说明原因——回滚就意味着删掉
  用户自己配出来的任务
- 验证：`pytest`（1581 passed / 51 skipped）、`ruff check src tests`、
  `ruff format --check src tests`、`mypy src`；在**生产库副本**上验
  `upgrade → downgrade → upgrade` 往返：21 张表逐表行数不变、
  `mission_tasks` 的 enabled/priority/params_json 逐字不变、
  `integrity_check` ok、`foreign_key_check` 空；另验了「同一 kind 有两行时
  downgrade 拒绝执行且一行不删」；用生产的 `var/mission-config-freezes.jsonl`
  （7 行，全部是旧格式）实测**全部 7 行仍读得出来**；调度台页面在临时库上实跑了
  一遍新建 / 改航线数 / 改出发星球被拒 / 删除；11 条变异逐条确认变红
- 安全：**没有任何一行代码去驱动游戏**——切换星球不在这个 PR 里。配一颗不是主星的
  出发星球会在**起子进程之前**被拒掉（页面 400 / 调度器就地停用并写清原因），
  一发都派不出去。绝不伪造 `attack_dispatches` 行；`mission_runs` 的历史行不随任务
  删除而删除。每条链路至少留一行任务，删光了页面上就再也建不回来
- 回滚：`alembic downgrade c1f70b8a26d4` + revert 本次提交（若已经建了第二个 bot
  任务，先自行决定留哪一行）
