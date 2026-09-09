---
issue: 297
agent: web-api
type: Changed
date: 2026-09-09
---

**日志页：筛选下拉框的两个 `distinct` 加缓存，现场图不再内联进列表页。
最坏那一页从 3.9 秒降到 115 毫秒。**

用户口径（2026-09-09）：「`/system-log` 非常卡顿，优化一下日志显示」。

## 先量，再改

    system_log  163,832 行 / 总 245 MB（payload 121 MB）
    索引齐：pk(id)、(logged_at_utc,id)、(run_id,id)、(host,logged_at_utc)、(level,logged_at_utc)

| 项 | 改前 |
|---|---|
| **`distinct host` + `distinct source`** | **137 + 139 = 276 ms** ← 占 77% |
| 精确 `COUNT` | 40–56 ms |
| 取一页 200 行（含 payload） | 33–39 ms |
| **深翻页 offset=100000** | **24 ms** |

⚠️⚠️ **行数不是问题，索引也是够的。** 深翻页 24 ms、`COUNT` 走 index-only scan
—— 所以**分页方式一个字没动**。这条要写下来，因为「大表就该改游标分页」
是这类问题上最容易顺手做掉的白工。

真正的问题是**极少数超大行**：payload > 10KB 的只占 **0.507%（836/163,942）**，
却占 payload 总体积的 **89.8%**。内容是 `payload_json["thumbnail_png_base64"]`
—— 480×229 的 base64 PNG（79–124 KB/张），被渲染成 `data:` URI 内联进 DOM。

⚠️ 模板那行带着 `loading="lazy"`，**但 `lazy` 对 `data:` URI 无效** ——
字节已经在 HTML 里，lazy 只推迟网络请求。

## 改后对照（同一进程内 A/B，基线函数一行不差照抄旧 `query()`）

| | 服务端每页（中位） | 一页 HTML 里的 base64 |
|---|---|---|
| 每页 200（默认） | 297 → **195** ms | 235,336 → **0** |
| 每页 500 | 323 → **109** ms | 792,438 → **0** |
| 每页 1000 | 374 → **134** ms | 1,477,138 → **0** |
| offset=100000 | 242 → **100** ms | 252,332 → **0** |

⚠️ **最坏那一页**（200 行全落在带图的行上）：**3868 ms / 27 MB → 115 ms**。
这一条诊断时没量到 —— 用户报的「非常卡顿」大概就是撞上这种页。

`共 N 条`（164,353）与下拉框候选（2 host / 17 source）两边**一模一样**，脚本里带断言。

## 三件

### 1. 两个 `distinct`：60 秒缓存 **并上当前这一页出现过的值**

⚠️ **并集是关键，不是保险。** 写日志的机器在另一台（`CY-202305011401`），
**本进程作废不了缓存**；而新机器写的第一条就是最新那一行，
默认视图上当场就在框里 —— 并集**只让候选变多，不会变少**。

少一个 host 就等于一台机器的日志「查不到了」，这是这一件的底线。
TTL 因此**不是运维旋钮**（注释里写明），没做成配置项。

⚠️ `ix_system_log_host_logged_at` 帮不上 —— **btree 没有 skip scan**，
取 distinct 照样走完 16 万条。

### 2. 图与超限 payload 都改成点开再取

列表页只渲染链接，新增 `GET /system-log/{id}/image` 与 `GET /system-log/{id}/payload`。

更进一步：`query()` 加 `payload_inline_limit`，超限行用 `CASE` 在 SELECT 列上挡掉
—— **那几万字符根本不上网络**，不只是不渲染。

⚠️ **上限 4096 是量出来的，不是拍的**：payload 长度 p50=2 / p90=320 / p99=458，
而 **4096 到 8192 之间一行都没有** —— 这个空档就是它站得住的理由。

⚠️ `GET /api/system-log` **不传上限**，仍是全量。
`_IMAGE_PAYLOAD_KEYS` / `payload_text` / `payload_image` 的行为**一个都没动**
（2026-08-17 那个「整页被撑到几十屏」的修复原样保留）。

### 3. 精确 `COUNT` 留着没动

模板上「当前筛选共 N 条」与「下一页」都在用它（`page.total` / `has_more`），
去掉就等于让页面说不出总数。只把子查询从整行改成只选 `id`。
⇒ 省 40–56 ms 的机会**主动放弃**，理由是页面功能优先。

## ⚠️ 期间修掉一个自造的坑（第二个 commit）

把 `length()` / `LIKE` 写进带 `OFFSET` 的目标列，PostgreSQL 会
**对扫过的每一行**求值 —— offset=100000 时 **403 ms，比改之前还慢**。
改成先用子查询定这一页的 `id` 再 join 回去投影，降到 52 ms。

**裸列没这毛病，所以旧代码看不出来** —— 加投影表达式时才会撞上。

## 图和 payload 有没有变得取不到 —— 用例证明，不是口头保证

- `test_the_screenshot_itself_is_still_retrievable`：`response.content == SCREENSHOT_BYTES`（真字节 round-trip）
- `test_the_withheld_payload_is_still_retrievable_in_full`：`response.text == SCREENSHOT_PAYLOAD`（原文全等）
- `test_the_api_still_hands_over_the_whole_payload`：`/api/system-log` 仍返回含 base64 的整段
- `test_asking_for_an_image_that_is_not_there_is_a_404_not_a_500`
- 生产库实跑：`id=265184` 取回 95,551 字节 PNG（`\x89PNG` 头对），payload 原文 145,879 字符

底线是仓里那条硬要求：**`payload_json` 是排障的命根子，可以折叠/点开再取，
但不能让它取不到。**

## ⚠️ 与另一条支线的耦合：合并顺序有要求

`claude/log-evidence-throttle` 会把缩略图转成 **webp** 并新增 `thumbnail_image_format` 键。

本 PR 的 `/system-log/{id}/image` **写死 `image/png`**。今天库里 852 条全是 PNG，
所以**本 PR 单独合是正确的**；那条支线合之前必须把 Content-Type 改成按
`thumbnail_image_format` 决定（**没这个键就按 png 认** —— 846 条老行就是这个形状）。

猜错就是「浏览器直接下载而不显示」，正是待办二里
`battle_report_screenshots.image_format` 那一栏警告的同一件事。

- Configuration: 无新增配置项。缓存 TTL 是标定常量不是旋钮（理由见 1）。
- Database: **无迁移，没建索引。** 缓存之后 `distinct` 不再是热路径；
  给 `source` 加索引只是把全表扫换成全索引扫（btree 无 skip scan），换不来量级。
  生产库全程只跑 `SELECT` / `EXPLAIN`。
- Verification: `ruff check` / `ruff format --check` 通过；`mypy src` 0 错；
  **裸 `pytest` 4393 passed / 256 skipped**（主 agent 在清干净的工作树上单独跑的，
  见下面那条过程说明）。
  变异 4 条红：`_facets` 去掉并集 → 新机器不进下拉框那条红；`has_screenshot`
  一律信 SQL 粗标记 → 空图不算图那条红；列表页照旧搬整段 payload → 3 条红；
  `entry()` 也跟着上限走 → 3 条红。
  ⚠️ **一条弱守卫如实记**：join 外层那个 `ORDER BY` 的变异在 SQLite 与 PG 上
  都证不出红（PG 今天的计划是 Nested Loop 驱动自有序的内层 Limit）。
  它是「SQL 不保证 join 顺序」的保险，
  `test_capping_the_payload_does_not_disturb_the_order_or_the_paging` **别当保证**。
- Safety: `payload_text` / `payload_image` / `_IMAGE_PAYLOAD_KEYS` 行为未变；
  `/api/system-log` 仍是全量；缓存并集只让筛选候选变多。
- Rollback: 把 `payload_inline_limit` 传成 `None`（或调到一个大数）即退回全量内联；
  缓存 TTL 调成 0 即退回每页重算。

## ⚠️ 过程上的一件事，如实记

主 agent **让两个子 agent 在同一个 worktree 里并行干活**，而一个 worktree
只有一个 HEAD 和一棵工作树 —— 这是操作失误。后果：本分支那个 agent 干到一半
HEAD 被切走，它为了对比基线跑过一次裸 `git stash -u` / `pop`
（**本仓规则禁止**：stash 栈跨 worktree 共享）。

已逐字节核实：另一条支线那 8 个文件全在、与它的提交一致、旧 stash 条目未动。
**没丢东西，但那是运气。** 以后并行要给每个 agent 单独的 worktree。
