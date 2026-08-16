---
issue: 55
agent: root
type: Added
date: 2026-08-16
---

新增 `system_log` 表：实机脚本与控制台的诊断输出集中入库，**换台机器也看得到**。

在这之前，实机那台机器上的 `print` 只落在它本地的 cmd 窗口和
`var/logs/mission-*.log` 里。而实机跑在一台机器上、人常在另一台机器上开控制台——
换台机器等于什么都看不到，「实机是不是卡住了」只能靠猜。

- Configuration: `EVO_HELPER_SYSTEM_LOG_RETENTION_DAYS`（默认 14）。
  0 或负数表示**不清理**，不是「全删」。
- Database: 新表 `system_log`，迁移 `a7f2c9d40b16`（down_revision `fa1c3d4e5f67`）。
- Verification: `pytest tests` 全绿；迁移在临时 SQLite 上 upgrade / downgrade /
  再 upgrade 都跑通，且与 `create_all` 建出来的表逐列比对一致。
- Safety: 写入走有界队列 + 后台线程批量刷盘，队列满丢最旧，DB 异常全部在 sink
  内部吞掉。**现有 print / 文件输出一行都没删**，这是双写。
- Rollback: `alembic downgrade fa1c3d4e5f67` 删表；没装出口时所有记录点都是空操作。

## 一、为什么不能同步写库

生产库在 Tailscale 内网的另一台机器上（PostgreSQL 18.6），一次 INSERT 就是一次
网络往返。而这些调用点全在实机点击循环里——海盗一轮半小时，光 `say()` 就有 80 个
调用点。把往返打进循环，等于给每次点击加一段时长不定的停顿，而 CLAUDE.md 的
反行为检测那条要求点击节奏必须是拟人化的随机分布。

所以：调用方只把记录塞进 `deque(maxlen=5000)`，立刻返回；后台线程按 200 条一批
刷盘；队列满了 deque 自己挤掉队头（**丢最旧**——出事那一刻的日志一定在队尾）。

实测（2026-08-16）：`emit` 2.9 µs/次；写入器每批睡 50 ms 时五千次 emit 共 0.014 s、
丢 0 条；库整个断线时两万次 emit 共 0.054 s、丢 11,880 条、41 批写入失败、
stderr 上只打 1 行（限流），一个异常都没漏给调用方。

⚠️ 最后一条不是「防御性编程」的口号。`tools/scan_coordinates.py` 的 `say()` 上方
记着 2026-08-10 那次事故：诊断路径上一行输出抛了 `UnicodeEncodeError`，把整个
runner 崩在半路，级联成整条链路停摆。写库的失败面比 `print` 大得多。

## 二、接入点

1. `say()`（`tools/scan_coordinates.py`）——实机脚本唯一的输出出口，改这一处
   覆盖 136 个调用点。`source` 从调用栈取，所以不用改任何调用点的签名。
2. `SystemLogHandler`（`logging.Handler` 子类），控制台启动时装上。
   ⚠️ 控制台**从来没调过** `configure_logging()`，所以 `mission_scheduler` 的两条
   `_LOGGER.info`、`vision/live_reports.py` 的 info 此前哪儿都到不了。
3. `tools/ranking_scan.py` 的 15 处裸 `print` 改成 `say()`，并给
   `game/ranking_nav.py` 注入同一个出口。

调度器起 runner 时把本轮 `run_id` / `task_id` / `mission_kind` 通过环境变量传下去，
子进程按继承认领——所以 `run_id` 那一列真的有值，页面能按轮次筛。

## 三、没有 `seq` 列

同一进程 FIFO 排队、后台线程按序批量刷盘，所以进程内 `id` 递增就是发生顺序。
再加一列序号只是给每条日志多摊一次写入成本。跨进程的先后由 `logged_at_utc`
回答——那是**产生时刻**，不是入库时刻，正是为了让刷盘延迟与网络抖动不改时间线。

⚠️ 主键写成 `BigInteger().with_variant(Integer, "sqlite")`。实测：纯
`BigInteger` 在 SQLite 上建出 `BIGINT`，不是 rowid 别名，插入当场
`IntegrityError: NOT NULL constraint failed`；PG 上两者都是 `BIGSERIAL`。

## 四、页面

新路由 `/system-log` + `GET /api/system-log`，按 level / source / host /
mission_kind / run_id / 时间范围 / 关键字筛选，服务端分页。

⚠️ **没有占用 `/logs`**：那一页是「攻击日志」，一行是一发打出去的舰队，读的是
`attack_intents ⟕ attack_dispatches ⟕ battle_reports`，与这张表毫无关系。
