---
issue: 241
agent: root
type: Changed
date: 2026-08-22
---

**军力榜的盲滚段从「慢拖 70 屏、每屏等 2 秒」改成「滚轮单格连拨、末尾统一等一次
滑行」，口径从「屏」统一到「行」。** 实测 294.6 秒 → 约 11 秒。

## 为什么现在能做（旧结论被推翻）

`docs/军力榜翻页-滚轮实测.md`（2026-08-19）的主结论是「**不能。滚轮一次调用最多走
约 14 像素，而一次慢拖走 400 像素**」。2026-08-22 实测**推翻了它**，那份文档已就地
改写（不留两份矛盾的）。

那个 14px 是真的，但它量的是**一个大 `dwData` 事件被游戏封顶**，不是滚轮的上限。
判据是**事件形状**，不是幅度 —— 首测发出去的事件里**没有一个是一格**：

| 发的 | 实际 `dwData` | 折合 | 列表推进 |
|---|---|---|---|
| 首测 `scroll(1)` | 1 | 1/120 格 | 1 px（记成了「1 格 = 1 像素」） |
| 首测 `scroll(800)` | 800 | 6.7 格 | 14 px（**单事件被封顶**，这条成立） |
| `scroll(-1)` × 80，间隔 16ms | −1 | 1/120 格 | 0–3 行 |
| **`dwData=-120` × 40，间隔 16ms** | **−120** | **1 格** | **44 行** |
| **`dwData=-120` × 80，间隔 16ms** | **−120** | **1 格** | **85 行** |
| 用户手动拨硬件 80 格 | −120 | 1 格 | 151 行 |

两条根因**都会静默失败**（事件发出去了、列表没走，代码侧看不出任何异常）：

1. **`pyautogui.scroll(n)` 在 Windows 上把 `n` 原样当 `dwData` 传给 `mouse_event`，
   不乘 120。** 一格的标准值是 120，所以 `scroll(1)` 只是 1/120 格。证据是底层鼠标
   钩子（`WH_MOUSE_LL`）：助手发 `scroll(-1)` 钩子看到 `delta=-1`，用户手动硬件是
   `delta=-120`。
2. **`pyautogui.PAUSE` 默认 0.1 秒**，把 16ms 的间隔撑成 117ms/格。游戏做的是
   **速度惯性**滚动，密度不够就攒不起动量（实测 80 格只走 2 行）。

基线（两次独立运行一致，且与生产日志 294.6 秒 / 70 屏吻合）：慢拖 `settle=2.0s`
= **7.3 行/拖、4.23 秒/拖、1.73 行/秒**；滚轮单格连拨 ≈ **19–23 行/秒**。

## ⚠️ 上线闸门：`ROWS_PER_NOTCH = 1.08` 只有 2 个样本

**2 个样本、1 台机器、1 次会话**（40 格→44 行 = 1.10；80 格→85 行 = 1.06）。
它一漂，「盲滚 700 行」实际走的就不是 700 行，而这个偏差是**静默**的 —— 采回来的数
只是少一截，页面上、日志里都看不出异常。

⇒ **上生产之前必须在实机复测至少 5 组**（格数覆盖 40/80/160/320/480），确认线性仍
成立、且 1.08 落在样本区间内。**复测不过就不上，先改标定值。**（本轮**没有**做这次
复测。）

## 落地的东西

| 层 | 改动 |
|---|---|
| `game/ranking_ui.py` | 新增 `WHEEL_DELTA=120`、`WHEEL_GAP_S=0.016`、`ROWS_PER_NOTCH=1.08`、`GLIDE_SETTLE_S=2.5`、`BLIND_SCROLL_ROWS=700`、`BLIND_SCROLL_MARGIN_ROWS`。**都是标定常量，不是运维旋钮** |
| `game/ranking_nav.py` | 原语 `wheel_notch()`（单事件恒为 `±120`）+ `spin_blind(rows) -> int`（返回实拨格数）。格数与间隔的循环放在 `game` 层，同 `_slow_drag` 分步的理由：放 `tools` 会让 `game` 反过来 import `tools` |
| `tools/scan_coordinates.py` | 驱动实现 `wheel_notch()`，显式 `pyautogui.PAUSE = 0` |
| `tools/ranking_scan.py` | 盲拖循环换成一次 `spin_blind_rows()`；行数记账（`BlindSpinAccount`）；日志改口径 |
| `domain/ranking.py` | 自标定与余量判据从屏改行；新判据 `is_bot_entry` |
| `storage/models.py` + `alembic/versions/b8e1c4a72f05` | 新列 `blind_scroll_rows` |
| `web/` + `settings.html` | 新旋钮 + 「≈N 秒」换算显示；旧「盲拖屏数」那一节改标题为「回滚用，当前不生效」 |
| `application/mission_scheduler.py` + `domain/missions.py` | 取值、校验、变化时记日志；`ranking_command` 换成 `--blind-rows` |

### 口径：一律用「行」

「屏」只是慢拖的副产品（1 屏 ≈ 7.3–8.3 行，而这个换算本身会飘），滚轮根本没有「屏」
这个概念，而名次天然就是行。配置里再经一道换算等于把误差腌进用户填的数里。屏退化为
**显示单位**。

⚠️ 代码里的 `ROWS_PER_SCROLL = 8.3` 是 2026-08-15 标的（8.0 与 8.3 两次），与本次量到
的 7.3 **不是同一次测量**；两个数并存、各有出处，余量换算取的是大的那个（更保守）。

### 改在循环这一层，不在 `scroll_blind()` 内部

只换内部而保留每次末尾的 `wait(SCROLL_SETTLE_WAIT_S)`，70 × 2 秒原样还在，等于白改。

```
原先：for _ in range(70): 慢拖一屏 + 等 2.0s              → 294.6s
现在：连拨 N 格（16ms 一格、中间一次都不等）+ 等一次 2.5s  → 约 11s
```

⇒ `SCROLL_SETTLE_WAIT_S`(2.0s) 现在**只剩检测段和采集段在用**，盲滚段一次都不碰它。

### ⚠️ 检测段与采集段一律不动，继续慢拖

滚轮会把列表停在**非整行位置**（实测偏离标定网格约 12px），而 `vision` 那边按
`ROW_FIRST_Y + k × ROW_PITCH_PX` **逐行裁剪** —— 偏了就横跨两行，名字全糊。实测过
一次：画面清晰，`rows_from_image` 只读出 2 个名次。**这是接线时最容易漏、漏了整段
读不出的一条。**

### 新 bot 判据 `is_bot_entry`：坐标 + 军力 ≠ 0

用户口径（2026-08-22）：「判断是否 bot 需要增加军力作匹配：id 符合 + 军力不等于 0」。
光看名字不够 —— `bot_` 前缀是玩家可以改名伪装的，而伪装的真人在军事榜上军力常年是 0。

⚠️ **`score is None`（军力读不出）照旧算 bot。** 只排除**明确的 0**。军力值本来就允许
读不出（用户口径 2026-08-14），拿「读不出」当排除依据会把大批真 bot 一起丢掉，而丢掉
的后果是那些坐标从此不再派兵、页面上看不出异常。所以判据写的是 `score != 0`
（`None != 0` 为真）而**不是** `score is not None and score != 0` —— 这不是漏了判空。

⚠️ **必须传 OCR 的原始读数，不能传插值后的值。** 流水线是「读分数 →
`descending_breaks` 把破坏降序的行丢成 None → `interpolate_scores` 补中点」，而
**插值补出来的值必然非零**（中点落在两个非零邻居之间）—— 那正好把这条判据要抓的信号
擦掉，擦掉之后它看起来只是「一个普通的低分 bot」。

这条判据**不往 `mentions_bot` 里加**：那个是检测段的廉价早期信号，只把名字列整条 OCR
一次，**那里根本拿不到军力值**。

### ⚠️ 不拿 `FIRST_BOT_RANK`(587) 当安全边界

用户口径（2026-08-22）：榜上那个「bot 起点」是**玩家改名伪装**出来的（判据只看名字
前缀，改名的真人一样命中），真 bot 区在更后面。所以 700 行**不越界**，而代码注释里
「40×12=480 < 587 所以到不了 bot 区」那套推理的前提已不成立。

⇒ **不拦、不标红、不据它算余量。** 拿一个被伪装污染的边界报警，比不报警更坏。盲滚
走多少行由用户在页面上定，助手不做越界判断。

## 日志（落 `system_log`，**不落文件**）

`source = tools.ranking_scan`，每轮盲滚一条。正文：

```
盲滚 N 行：发了 M 格、拨完用 X.X 秒，每格实测 R 行（标定 1.08）
```

`payload_json`：`rows_requested` / `notches_sent` / `spin_seconds` /
`glide_seconds` / `rows_measured` / `rows_per_notch_observed` /
`rows_per_notch_calibrated` / `rows_to_bot_area` / `source`（手填还是默认）。

⚠️ **`rows_per_notch_observed` 是这条日志的要害**：标定只有 2 个样本，它一旦漂了，
盲滚距离就静默地变了。把每轮的实测值记进库，才能在事后回答「这个标定还成不成立」，
而不是等到某天发现 bot 少采了一截再回头查。`rows_requested` 与 `notches_sent`
**都要留**：格数是真发生的事，行数是它乘标定算出来的，只留折合值就把两者的差别抹平了。

⚠️ 这条日志落在**检测段跑完之后**而不是拨完那一刻 —— `rows_to_bot_area` 要等检测段
才知道，而它和「每格实测几行」放在同一条里才对得上账。

⚠️ `rows_measured` 是**尽力而为**：滚轮把列表停在非整行位置，逐行裁剪读出来的名次会
横跨两行，读不出就 `None`。它**只喂日志、不参与任何判据**，所以「读不准」的代价只是
这一趟答不出「每格走了几行」。

- Configuration: 新旋钮 **`military_attack_config.blind_scroll_rows`**（攻击配置页
  「军力榜盲滚行数」）。**留空 = 按实测自动标定**（最近 `BLIND_SCROLL_SAMPLES` 次
  「翻了 N 行到达 bot 区」取**最小值**再减 `BLIND_SCROLL_MARGIN_ROWS` 行余量；历史
  不足时用代码默认 **700 行**）；**填了数就锁死**，不再自动调。**0 合法**（「一格都
  不拨」是最保守取值），负数不合法，**上界不设**。
  ⚠️ **置空 ≠ 退回慢拖**，走的仍是滚轮 —— 与本表其余可空旋钮的惯例一致（NULL = 跟着
  代码默认走）。退回慢拖是**命令行层**的事，见 Rollback。
  旧的「军力榜盲拖屏数」（`blind_scrolls`，屏）**保留不动**，页面上那一节改标题为
  「回滚用，当前不生效」并写明填什么都不上命令行。
  `WHEEL_DELTA` / `WHEEL_GAP_S` / `ROWS_PER_NOTCH` / `GLIDE_SETTLE_S` 一律是
  **标定常量不是运维旋钮**（判据见 CLAUDE.md：「改这个值会让结果变更适合我，还是
  变错？」）—— 它们量的是「多密才攒得起动量」「一格推进多少行」，调了不会更适合谁，
  只会让盲滚静默地走不动或走错距离。
- Database: 迁移 **`b8e1c4a72f05`**（`Revises: a3c81f5d2b64`，保持单一 head），
  `ALTER TABLE military_attack_config ADD COLUMN blind_scroll_rows INTEGER NULL`。
  ⚠️ **刻意不给 `server_default`** ——照上一列 `blind_scrolls`（`c2a8f4d31e75`）的先例：
  给了默认值就分不开「没配」和「恰好配成了当前默认」，日后把 700 调成别的数时，存量
  那一行会被钉死在 700 上，而它表达的其实是「跟着默认走」。NULL 也正是升级完成那一刻
  行为完全不变的保证。可空列的 `ADD COLUMN` 两种方言（SQLite / PostgreSQL）都直接
  支持，不必走 `batch_alter_table` 重建整张表。
  ⚠️ **本轮没有对任何生产库执行 `alembic upgrade`**，也没有连生产库；迁移由生产自己
  在重启时升（`web.runtime._upgrade_database`）。
- Verification: 单元 —— `tests/unit/game/test_ranking_wheel_constants.py`（标定常量）、
  `tests/unit/game/test_ranking_spin_blind.py`（行→格换算，边界 0 / 1 / 很大的值）、
  `tests/unit/tools/test_live_driver_wheel.py`（`dwData` 恒为 ±120、间隔不小于
  `WHEEL_GAP_S`、`PAUSE` 被置 0）、`tests/unit/tools/test_ranking_blind_spin_account.py`、
  `tests/unit/domain/test_ranking.py`（`is_bot_entry`，含 `None` 放行那一档）、
  `tests/unit/domain/test_missions.py`（`--blind-rows`）。集成 ——
  `tests/integration/application/test_ranking_blind_rows.py`、
  `test_ranking_blind_scroll_log.py`、`tests/integration/api/test_scheduler_api.py`
  （新旋钮进「所有旋钮一起存一起读」那张表）。迁移 ——
  `tests/integration/storage/test_blind_scroll_rows_migration.py`（升级/降级各跑一次，
  存量行为 NULL）。
  ⚠️ **两处仍然没验**：(1) **实机一趟都没跑过** —— 全部改动只有代码级断言，而
  `ROWS_PER_NOTCH` 的复测（上线闸门）需要实机；(2) 页面没有渲染验证（「≈N 秒」那行
  换算、旧屏数那一节的新标题），本轮不许起 preview / dev server。
- Safety: **盲滚段一次点击都不发**，`allow_actions` 全程为假；不改攻击链路、不改采集
  口径、不动任何与派遣有关的东西。四条不变量：**只有盲滚段用滚轮**；**单个事件不许
  超过一格**（大 delta 会被游戏封顶，而封顶是静默的）；**`pyautogui.PAUSE` 必须显式
  置 0**（否则间隔被撑成 117ms、动量攒不起来，症状同样是「拨了但没走」）；**拨完必须
  等滑行停**（`GLIDE_SETTLE_S`，实测惯性 1.6–2.3 秒有界，取 2.5 留余量），否则检测段
  会在移动中的画面上读行。
  ⚠️ 方向上的安全性来自「少滚只是多滚几屏」：盲滚之后紧跟着检测段逐屏确认「到 bot 区
  了没有」，少滚由它接手；**多滚才是危险的** —— 会跳过榜首那批军力最高的 bot，而漏采
  是**静默的**（不报错、不少日志，只是采回来的数少一截）。
- Rollback: **不需要迁移、不需要改代码、不需要重新发版** —— 命令行上只给
  `--blind-scrolls` 不给 `--blind-rows`，`scan()` 内部的 `blind_rows` 就是 `None`，
  整段退回慢拖那条老路（优先级是**显式写出来**的，不靠「谁不是 None」撞出来；两个都给
  时行数赢，因为那才是主路，回滚要显式选）。`blind_scrolls` 那一列与页面上那个框正是
  为此**保留不删**的落脚点，顺手清理掉它回滚就变成「改代码 + 重新发版」。
  代码级回滚：把 `mission_scheduler` 两处 `_blind_rows()` 换回 `_blind_scrolls()`、
  `ranking_command` 的参数换回 `--blind-scrolls`。库里已写下的 `blind_scroll_rows`
  值 revert 之后只是一列没人读的整数，不会让任何查询报错。
