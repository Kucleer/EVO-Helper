---
issue: 193
agent: domain-storage
type: Added
date: 2026-08-18
---

新增离线入口 `tools.reread_report_resources`：拿 `battle_report_screenshots` 里**已经存着的**
战报面板重跑「获得资源」12 格，回填 `battle_report_resources`。**默认只打印，不写库。**

## 为什么值得单独做一件事

库里躺着 34 份战报的面板图，而 `battle_report_resources` 只有 5 份有明细——另外 29 份当年
「12 格没读全」，按全有或全无整块作废了。PR #191 把那 12 格改成字模匹配之后，**同一批图
34 份全部读得全**。图还在，所以不用回游戏、不用碰鼠标，离线重跑一遍就能把那 29 份补回来。

`docs/选靶数据跟踪-待办.md` 里三条「样本不够」中的两条（第 4、7 条）等的就是这批数据：
稀有材料的样本会从 5 份变成几十份。

## 为什么是新入口，不是给 `tools.backfill_reports` 加个开关

那个入口做的是**回游戏信箱重翻**：起 Chrome、点邮件、真点鼠标，跑之前调度器必须停着，
工作时间还不许起游戏。这一条是**离线**的——像素早就在库里了，全程不碰游戏、不动鼠标、
不开窗口。并在一个命令下的代价是那条硬约束会跟着套到本来不需要它的路径上，或者反过来
被人误以为不需要。

## ⚠️ 全有或全无一个字都没放松

12 格但凡有一格读不出，整份跳过、一格都不写、更不补 0（判据仍在
`domain.battle_resources.parse_resource_grid`）。库里只存非零行，「没有这一格 = 这一格是 0」
这条语义只在 12 格全读到时才成立；放松了的话，一次读不全会变成几个凭空捏造的零，而且
库里看不出来。**这次要提高的是读得出，不是降低要求。**

## 三样新东西

- `storage.report_screenshots.list_refs()`：图的清单，**不取字节**（这张表按保留期最多能到
  近百 MB，数一数不该把它全拉回来）。
- `vision.optional.panel_resources`：把 12 格 ROI 从视口坐标**平移**到面板坐标，在存档面板上
  重读。尺寸不符一律拒读——按常量硬裁会读出一屏像模像样的错数。
- `storage.report_resources`：`battle_report_resources` 的读回与**逐格**改写。逐格而不是
  「整份删了重写」，于是打印出来的是哪几格、落库的就是哪几格，且天然幂等。

- Configuration: 无新旋钮。`--apply` 是开关不是偏好（默认干跑），`--database-url` 只是
  让调试能指到测试库；识别用的门槛与 ROI 全是标定常量，调了就是错，照旧不做配置
- Database: **无迁移、无 schema 变更**。只往 `battle_report_resources` 写行；
  `battle_reports` 的 `outcome` / 双方单位 / 双方损失 / `match_status` / `dispatch_id`
  一个字段都不碰（用例钉着）
- Verification: `pytest`（2914 passed / 106 skipped）、`ruff check src tests`、
  `ruff format --check src tests`、`mypy src`；三处变异逐条确认转红（见 PR 正文）
- Safety: 默认干跑，连 `system_log` 都不写（它和数据住同一个库）；`--apply` 才写，
  且每一处改动连同「哪份战报、哪一格、旧值 → 新值」写进 `system_log`；
  不碰游戏、不动鼠标、不删图、不改图；夹具是合成的，一张游戏截图都没进仓库
- Rollback: revert 本次提交即可（没有迁移要退）。已经写进 `battle_report_resources` 的行
  按 `system_log` 里 `source = 'resource-reread'` 那几条逐格回得去
