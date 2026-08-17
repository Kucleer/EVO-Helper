---
issue: 172
agent: vision-game
type: Fixed
date: 2026-08-17
---

登录/加载中间态不再被当成「画面认不出」：恢复阶梯上新增一级「先当成登录还没走完，
等它自己变成 START」，等不到才退回原来那条路。

## 现象

runner CY-202305011401，2026-08-17 傍晚，`tools.scan_coordinates` 连报：

    17:xx      尺寸 (1920, 917)；导航条读到 '> =. _'；入口标题读到 ''
    18:04:43   尺寸 (1920, 917)；导航条读到 ''；入口标题读到 'TAL pisE'

两条是同一屏的两个瞬间：一次导航条读到噪声而入口标题空，一次反过来。

## 根因

游戏的登录流程更新了，登录页翻到 START 的那几秒读出来是花的，落到 `UNKNOWN`。
旁证：那串 `'>  =.  _'` 在 `make_session_keeper.observe` 的注释里 2026-08-11 就
记着，是入口页明暗动画的暗相；`'TAL pisE'` 是 `ENTRY_TITLE_ROI` 上 `ETERNAL VOID`
淡入淡出的残片。也就是说这不是一屏全新的画面，而是那段「读不清」的窗口拉长到
`OBSERVE_FRAMES = 4` 帧兜不住了。

而 `UNKNOWN` 会一路走完恢复阶梯：关浮层 → **关掉 Chrome 重开**。登录才到一半就关窗，
救不了，还把本来马上就好的会话亲手弄坏，并吃掉一次「3 次 / 滚动 1 小时」的重开配额。

## 判据

**不看 OCR 残片长什么样**——每帧读出来的碎字都不一样，写死必然失效。判据只有一条：
再等一会儿，它会不会**自己**变成认得出的一屏。会 → 中间态；不会 → 真认不出。

等待复用 `SessionKeeper` 已有的轮询 `_wait_for`（那套循环里已经写着「轮询期间容忍
UNKNOWN 继续等，超时才算失败」），新增 `SessionKeeper.wait_for_known_screen()`。
阶梯里新的一级 `scan_coordinates.wait_for_login_if_unrecognised` 排在
**关浮层之后、关窗重开之前**：浮层是更常见的那一种且几秒就能证否，
而这一级要挡住的正是关窗重开那一下。三条链路（scan / ranking / pirate）共用同一份。

## 超时上限

`LOGIN_SETTLE_TIMEOUT_S = 90.0`。**必须有上限**：没有尽头的等待会把一个真坏了的画面
变成整夜静默空转（整段在 `domain.scheduler.exit_code_for_environment_fault`）。
超时后原样退回「认不出」，关窗重开、配额、豁免、自动停用一样不少。

取值依据：与 `START_LOAD_TIMEOUT_S` 同量级——两者等的都是**游戏自己的加载**，
而不是 Chrome 冷启动（`RESTART_ENTRY_TIMEOUT_S = 120`）；上界另有硬约束，必须远小于
`HEALTH_CHECK_INTERVAL_S = 600`，否则一次等待就吞掉一整个巡检周期。

- Configuration: **无新增配置项，有意为之。** 这是**标定常量**不是运维旋钮——它编码的是
  「游戏自己把 START 画出来要多久」，调小会把正常登录判成故障、调大只会让坏画面更晚进
  恢复阶梯，两边都是错，没有一边是「更适合我」。同族的 `ENTRY_TIMEOUT_S` /
  `START_LOAD_TIMEOUT_S` / `RESTART_ENTRY_TIMEOUT_S` 也都是硬编码的，单把第四个做成
  可配置只会让人以为前三个也能调。注释里写了「真要改，先量一遍登录页翻到 START 实际
  花多久」。
- Database: **无迁移。**
- Verification: 2671 passed / 82 skipped（起点 2651）；`ruff check` + `ruff format --check`
  在 `src tests` 上全绿；`mypy src` 122 源文件零问题。（仓库根目录的 `ruff check .` /
  `format --check .` 在 `origin/main` 上本来就有 4 个 error + 9 个待格式化文件，
  全在 `alembic/versions/` 与 `docs/*.md`，本分支未增未减也未顺手改。）
  四组变异各验过一次（改坏 → 转红 → 还原 → 复跑全绿）：
  ① 超时分支删掉（`if state not in SETTLED_SCREENS` → `if False`）→ 1 条红；
  ② `LOGIN_SETTLE_TIMEOUT_S = 1_000_000`（等于永远等下去）→ 3 条红；
  ③ `wait_for_login_if_unrecognised` 直接返回入参（退回改动前行为）→ 6 条红；
  ④ 把 `'TAL PISE'` 加进 `ENTRY_WEAK_MARKERS`（按字面量匹配）→ 1 条红。
- Safety: **没有新增任何点击、派遣或写库路径。** 新增的这一级只观察、只等待，
  是整条恢复阶梯里第二级完全不在认不出的画面上动手的。全程未启动游戏、未动鼠标键盘、
  未连生产库。
- Rollback: 纯代码回滚即可，没有数据形状变化。
