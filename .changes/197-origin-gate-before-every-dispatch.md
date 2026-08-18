---
issue: 197
agent: vision-game
type: Fixed
date: 2026-08-18
---

**每一发派遣之前回读派遣面板的「起点」**，对不上（含读不出）就不派、停下这一轮；
外加**关窗重开之后把「本轮已经切到哪」这份记忆作废**。前者是兜底，后者是止血。

## 事故

生产 2026-08-18：调度器决定 `--origin 9:250:8`，库里也按 9:250:8 记账，
同一轮的第二发却是从主星 4:277:15 打出去的。飞行时间是硬证据（用户已核）：

| 时刻 | 目标 | 实际 | 从 9:250:8 应 | 从 4:277:15 应 | 判 |
|---|---|---|---|---|---|
| 18:53:32 | 9:231:7 | 18.5 分 | 18.6 分 | 125.0 分 | 从 9:250:8（误差 0.5%） |
| 18:56:22 | 9:205:14 | 125.0 分 | 22.5 分 | 125.0 分 | **从 4:277:15**（误差 0%） |

代价两层：一发白占 3.4 小时航线（往返 45 分钟 → 250 分钟）；更贵的是**账是错的**
——#179 那两道航线闸按 9:250:8 扣，实际占的是 4:277:15 的额度。

## 触发点（只读翻生产 `system_log` 找到的，不是猜的）

```
18:52:07  起点回读 '9:250:8'，确认当前星球是 9:250:8
18:53:32  已发动攻击 → 9:231:7（预设 AAA）        ← 确实从 9:250:8
18:54:59  派出之后切不回恒星系视图；关窗重开一次再试（兜底策略）
18:55:34  重开之后已经重新进到游戏内
18:56:22  已发动攻击 → 9:205:14（预设 BBB）       ← 已经是主星
```

关窗重开的是整个 Chrome 窗口，游戏重新走一遍入口序列，**落点是主星**。而
`_require_system_view` 那一支原先只清导航器缓存，`_current_planet` 一个字没动，
于是 `switch_needed` 说「本轮已经切到 9:250:8，不用切」。

**另外两处关窗重开都清了**（`_ensure_session` 的重连支、`_mailbox_restart`），
那边的注释甚至逐字预言了这次的后果。三处共用一件事而只改了两处，代价就是这个。

## 兜底：`PirateLoop._require_origin_before_dispatch`

`attack()` / `scout()` 里，派遣面板一铺开就回读「起点」，与
`_options.origin or origin()` 比对——**期望值取的是记账用的那个表达式**
（`_record_intent` 写进 `attack_intents.origin_*` 的就是它），不是 runner 自己的
`_current_planet`：拿记忆比记忆是同义反复，正是这次失效的那半边。

- 不一致或读不出 → 不派、记 `refused`、留一帧现场（封顶）、把「期望哪颗 / 读到哪颗 /
  原文 / ROI」连同缩略图写进 `system_log`（落库不落文件：实机在另一台机器上）、
  关面板、清导航缓存与出发星球记忆，抛 `OriginDrifted`。
- **读不出重读 `ORIGIN_SETTLE_TRIES` 轮，仍读不出按「核不过」收场，绝不当成一致。**
  会动的画面上单帧的空结果是抛硬币（同 `read_panel_confirming` /
  `read_names_confirming`）。
- `OriginDrifted` 挂在 `RoundExhausted` 底下，要的是那套善后：退出码 0、
  不计连续失败、不自动停用（用户口径「不停用、不记失败」）。停整轮而不是跳过单个
  目标，因为起点是整轮共用的状态——一发对不上，余下每一发都会对不上。

## 起点 ROI：复用 `pirate_ui.FLEET_ORIGIN_ROI`，没有新标

**简报页上没有起点坐标。** 逐张看过 `var/logs/calib-侦察-3-简报页-viewport.png` 与
`var/logs/dump-briefing-*.png`（08-13 至 08-16 共 50 余张）：那一屏只有任务类型 /
速度 / 飞行时间 / 预计到达 / 气体消耗 / 货舱容量六行。而没标定过的 ROI 一定读成空、
空又算核不过——猜一个框写进去的后果是**每一发都被拦下**，比事故本身还糟。

派遣面板那一行是实拍核对过的，且三条路径共用同一个框：`calib-舰队面板-client.png`
（底部导航「舰队」，原标定）、`calib-1-dispatch.png`（攻击链路）、
`calib-侦察-2-派遣面板-viewport.png`（侦察链路，viewport 空间差 38px 标题栏）。

⚠️ 闸门必须排在**展开预设条之前**：`PRESET_TOGGLE` 就坐在起点那一行右端，条一展开
就把它整个盖住（实拍 `var/logs/atk-2-presets.png`）。这条单独有用例钉着。

## 没做的那一半

用户提的「找出触发点就在那之后主动重切一次」只做了安全的一半（作废记忆），
**没有做当场重切**：`ensure_origin_planet` 自己就调用 `_require_system_view`，
在里面回调会递归；挂到两个 runner 的每目标循环上是热路径改动，本机无法实机验证。
记忆作废之后闸门会在下一发当场拦下并停轮，下一轮开工重新切一次。留作后续。

- Configuration: **`ORIGIN_SETTLE_TRIES` 不做成可配置**，注释里写明「这不是偏好项」。
  按仓库口径判：取值由「面板滑进来要多久」这条画面几何决定，不取决于用户处境；
  调大只让真故障多拖几秒，调小会让正常的一帧空读把整轮停掉——改它会让结果变**错**，
  所以是标定常量而不是运维旋钮。`MAX_ORIGIN_DUMPS` 同理（只影响排障时有几张图）。
- Database: **无迁移、无 schema 变更。** 生产库只跑过一次 `SELECT`，事务先
  `SET default_transaction_read_only = on`；测试库没碰。
- Verification: `pytest -q` **2899 passed / 106 skipped**（skipped 全是缺实拍或缺
  Tesseract 的 `*_live.py`，与本次无关）；`ruff check src tests`、
  `ruff format --check src tests`、`mypy src`、`python -m compileall src tests` 全绿。
  三处变异测试（去掉核对 / 核不过照打 / 读不出当成一致）分别转红 15 / 13 / 3 条，
  基线与三次还原后均为 `tests/unit` 1809 passed。
  **全程没启动游戏、没动鼠标键盘、没起 runner；一张图都没进 git。**
- Safety: 这道闸门只会**少派**，不会多派——任何拿不准都落在「不派」那一侧。
  它不点任何新坐标（只多读一块已标定的 ROI），拦下时走的是既有的
  「关派遣面板 + 清导航缓存」那条路。停轮走 `RoundExhausted` 一档，
  不计连续失败、不触发自动停用。
- Rollback: 两条改动互不相干，可单独回退。回退兜底：删掉 `attack()` / `scout()` 里
  那两行 `_require_origin_before_dispatch(coordinate)` 及三个私有方法、
  `OriginDrifted`、两个常量。回退止血：删掉 `_require_system_view` 里那行
  `self._current_planet = None`。`domain.planet_switch.origin_in` 是从
  `origin_confirmed` 里拆出的同一份解析器，两条都回退时可以留着。
