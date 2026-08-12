# 从 SQLite 换到 Postgres：完整方案

**场景**（用户 2026-08-11）：两台机器。工作机有 AI 工具、做开发调试、不能 24 小时开；
个人机常开、用来挂机、但缺环境。目标是个人机跑实机，工作机随时看到实时数据。

**一个决定性前提**：只有一个游戏账号，所以**任何时刻只可能有一台机器在跑自动化**。
海盗每天 32 次是账号级硬限制，两台同时跑不是「数据冲突」而是真的超额、舰队被强制返回。
所以这是**单写多读**，不是双向同步——下面所有设计都建立在这条上。

---

## 一、为什么不能用 SQL dump 直接迁

已核实的两处类型差异，任一处都会让「导出 SQL、导入 Postgres」这条路出错：

| | SQLite 里的样子 | Postgres 里的样子 |
|---|---|---|
| `Uuid` × 21 列 | 32 位十六进制字符串 | 原生 `uuid` 类型 |
| `Boolean` × 9 列 | 整数 0 / 1 | 真布尔 `true` / `false` |

SQLAlchemy 的 `Uuid` / `Boolean` 是**方言无关类型**——它在读写两端各自做转换。
所以迁移必须**走 ORM**：用 SQLAlchemy 从 SQLite 读成 Python 对象，再用 SQLAlchemy
写进 Postgres。中间那一步的类型转换由它负责，我们不手写。

---

## 二、时区：已经做完了（PR #104），但这一节记着当初判断错的地方

**结论先说：这件事已经落地，迁移时不用再操心。** 保留这一节是因为我最初写的判断
有三处是错的，而其中一处如果照着做会把库弄坏。

### 我当初写错的三处

| 我写的 | 实际 |
|---|---|
| 33 个 `DateTime` 列 | **34 个** |
| `models.py` 里那 33 处要改 | `models.py` 里**没有一个裸 `DateTime`**——34 列全部走 `storage/database.py` 的 `UTCDateTime(TypeDecorator)`。所以「改 33 处」实际是**改一处**，`models.py` 一行没动 |
| SQLite 把 tzinfo 一起存进去，读出来还是带时区的 | **不是**。SQLAlchemy 的 SQLite 方言 `DATETIME` 绑定格式里没有时区字段，偏移量被**丢掉且不换算**——实测 `03:04:05+08:00` 落盘成 `03:04:05`，比真实 UTC 早 8 小时 |

由第二、三条推出一个更要紧的更正：**改之前的代码在 Postgres 上其实就是对的。**
那个 `UTCDateTime` 一直在写入时先 `astimezone(UTC)` 再剥 tzinfo、读出时补回 `tzinfo=UTC`，
naive 列配 naive 绑定前后自洽。我说的「读出来变成 naive」不会发生。

### ⚠️ 真正的风险是「只做一半」

如果照我最初的字面意思——无差别给列加 `timezone=True`、而**不动绑定**——naive 值会遇上
`TIMESTAMPTZ`，Postgres 会拿**会话时区**去解释它：服务器不在 UTC 就整库偏时差，
而且照旧不报错。PR #104 是两半一起改的，并有测试钉死。

所以：**迁 Postgres 之前不要再动这块**。要动的话两半必须一起动。

### 顺带查实的两件事

- **`daily_reconciliations.day_utc` 不是 datetime**，它是 `String(10)`——不在这 34 列里。
  唯一名字像日期的是 `run_instances.target_date`，而它是**死列**（生产两处建 `RunInstance`
  都不写它，全仓无人读）。
- 原先这块是**测试盲区**：没有一个测试直接覆盖 `UTCDateTime` 的往返。现在有 9 条
  （`tests/integration/storage/test_utc_timestamps.py`）。

### 一条对写测试的提醒

`replace(tzinfo=UTC)` 与 `astimezone(UTC)` 对 naive 值**在 UTC 主机上是同一个函数**，
而 CI 是 UTC 的 Linux。所以「把 astimezone 换成 replace」这种变异在 CI 上**必绿**——
不是断言写弱了，是差别在那个环境里**不可观测**。要验它必须先把进程时区掰到非 UTC。

---

## 三、执行步骤

### 0. 前置：个人机装环境（与 Postgres 无关，先做）

```
uv sync --extra vision --extra live
```

`uv.lock` 已进版本管理（PR #64），装出来和工作机一致。另外三处按机器改，都有环境变量：

- `EVO_HELPER_TESSERACT_PATH`
- `EVO_HELPER_CHROME_PATH`
- 页面 DPR（见 `config.py`）

### 1. 加驱动依赖

`pyproject.toml` 里加一组：

```
db = ["psycopg[binary]>=3.2,<4"]
```

单开一组而不是塞进主依赖：工作机不挂机时不需要它，CI 也不需要。

### 2. 装 Postgres 到**个人机**（那台常开的）

建库、建用户。连接串形如：

```
EVO_HELPER_DATABASE_URL=postgresql+psycopg://用户:密码@主机:5432/evo
```

⚠️ 别让它裸奔在公网上。两台机器之间用 **Tailscale**（或等价的私有网络）连，
Postgres 只监听那个虚拟网卡。这个项目的控制台默认绑 `0.0.0.0` 已经是「可信内网」的假设了，
数据库不要再放宽一层。

### 3. 建表

```
EVO_HELPER_DATABASE_URL=... .venv/Scripts/python.exe -m alembic upgrade head
```

⚠️ **这一步现在会挂。** 迁移 `8c41b9d201ff` 用了 `lower(hex(randomblob(16)))`，
那是 SQLite 专有函数，Postgres 上不存在。迁库之前必须先处理它——改成方言无关的写法，
或者给它加一个按方言分支的实现。**这是 PG 迁移眼下唯一已知的硬拦路虎**（PR #104 顺带查出来的）。

⚠️ 从**空库**跑全套迁移，不要从 SQLite 抄结构。迁移里那几处 `batch_alter_table`
在 Postgres 上照样能跑（它是 SQLite 的补丁，在 PG 上退化成普通 `ALTER`）。

⚠️ 跑完核对 `alembic_version` 与工作机一致。两台机器的代码版本必须同步——
**库结构跟着代码走，不跟着机器走**。

### 4. 搬数据（ORM 级，一次性脚本）

思路：对每一张表，从 SQLite 的 session 读出 ORM 对象，`expunge` 之后写进 Postgres 的 session。
**按外键依赖顺序**搬，否则会撞外键约束。

依赖顺序（从被依赖的往依赖的排）：

```
scan_plans → run_instances → attack_intents → attack_dispatches → battle_reports
                                                    ↘ fleet_snapshots
coordinate_scans / bot_targets / scan_ranges / mission_tasks / scheduler_config
mission_runs / daily_reconciliations / scout_reports → scout_trigger_ships
state_events / ui_observations / artifacts / intel_filters / target_revisits
```

搬完**逐表核对行数**，和迁移前的快照对齐。当前量级（供对账参考）：
`coordinate_scans` / `bot_targets` 各 4255、`attack_intents` ~250、`attack_dispatches` ~190、
`scout_reports` ~98、`battle_reports` 数十。

⚠️ 搬之前先 `VACUUM INTO` 出一份 SQLite 快照，**对快照搬**，别对正在被写的库搬。

### 5. 验证（缺一不可）

1. **逐表行数一致**
2. **抽样核对时区**：挑几条 `attack_dispatches`，确认 `dispatched_at_utc`
   在两边是**同一个时刻**。注意比的是时刻不是字符串——SQLite 那边落盘的是 naive UTC，
   PG 那边是 `timestamptz`，字符串长得不一样但代表同一刻才对。差 8 小时就是错的。
3. **`pytest tests -q` 全绿**（测试用临时库，验的是代码没被改坏）
4. **控制台起得来**，攻击日志 / 情报中心 / 任务中心三页都能开，数据对得上
5. **实机跑一轮**只读的（`pirate_loop --systems 2:137`，不带 `--scout --attack`），
   确认开工对账、读战报、写库整条通

### 6. 切换与回退

- 个人机的 `.env` 指向 Postgres，工作机同样指过去。
- **SQLite 那个文件留着别删**，至少留一个月。回退就是把 `EVO_HELPER_DATABASE_URL`
  改回去——前提是这期间新数据都在 PG 上，回退会丢掉那部分。所以真要回退，越早越好。

---

## 四、迁移之后要注意的

### 测试仍然跑 SQLite

CI 在 Linux 上跑，用的是临时 SQLite 库。**不要为了「和生产一致」把 CI 也改成 PG**——
那会让每次 CI 都要起一个数据库服务，换来的是跑得更慢、更容易挂。

代价是：**SQLite 上绿、Postgres 上未必绿**。已知的差异面：

- 类型严格性（PG 严，SQLite 宽松）
- 事务隔离与锁行为
- 大小写敏感的排序

所以第 5 步那几条验证不能省，尤其「实机跑一轮」——它是唯一真的在 PG 上跑生产代码路径的那次。

### 并发写仍然只许一台

Postgres 能扛并发写，但**游戏账号扛不住**。工作机连过去是为了**读**；
真要在工作机上跑实机，先确认个人机的调度器已经停了。

这一条没有技术手段能挡住（两台都能写库），只能靠约定。如果哪天想让它变成硬约束，
可以在 `scheduler_config` 里加一行「当前持有者」，起调度器时抢占、退出时释放——
但那是另一件事，不在这次范围里。

### `var/` 里的东西不会跟着走

`var/logs/` 的截图与日志、`var/mission-config-freezes.jsonl` 的配置固化记录，
都是**文件**，不在库里。工作机想看这些还是要单独同步。

---

## 五、什么时候**不**该做这件事

如果你现在真正缺的是「个人机能跑起来」，而不是「工作机能实时看数据」，
那就先只做第 0 步（装环境），数据用 `VACUUM INTO` 出快照传过去看。

理由：Postgres 迁移会同时引入三个新变量——新的数据库、新的时区语义、新的网络依赖。
在挂机本身还没稳定跑通一两周之前引入它们，出了问题很难分清是哪一个的锅。
等实机稳定了再迁，那时迁的是一个已知良好的库。
