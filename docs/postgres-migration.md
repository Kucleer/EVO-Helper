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

## 二、⚠️ 最大的坑：33 个 `DateTime` 列没有一个是 `timezone=True`

这是**动手之前必须先定的一件事**，不是迁移过程中的细节。

现状：`models.py` 里 33 个 `DateTime` 列全部没写 `timezone=True`，而代码里存进去的是
**带时区的 UTC 时刻**（`datetime.now(UTC)`）。

- **SQLite 不在乎**：它把 datetime 存成字符串，tzinfo 跟着一起进去，读出来还是带时区的。
- **Postgres 会当场丢掉**：`TIMESTAMP WITHOUT TIME ZONE` 会把 tzinfo **静默截掉**，
  读出来变成 naive。

后果不是报错，是**安静地错**。这个项目已经被时区坑过至少三次：

- 战报页眉时间当成 UTC+8 解析，硬减 8 小时，把当天早上的报告算成前一天，
  「读到前一天就收工」的判据当场误触发，一封都没开；
- `--round-started-at` 不带时区会让上一轮的派遣被算进本轮；
- 攻击日志的日期筛选必须按 UTC+0 切，按 UTC+8 切会让跨日那几小时归错天，
  而海盗每天 32 次的边界正好在那里。

naive 的 datetime 进了库，上面每一条判据都会重新变得可疑，**而且不报错**。

**两条路，二选一，写进决定再动手：**

### 路 A（推荐）：迁移前先把列改成 `timezone=True`

- 改 `models.py` 那 33 处，加一个 alembic 迁移。
- 在**现有 SQLite 库**上先跑通、跑测试、实机跑一轮，确认没坏。
- 然后再做 Postgres 迁移。

好处是把「换库」和「改时区语义」两件事**分开验证**。混在一起做，出了问题分不清是哪一半的锅。

### 路 B：接受 naive-UTC

全库统一存 naive UTC，读出来再补 `tzinfo=UTC`。省一次迁移，但从此每个读取点都要记得补，
漏一处就是一个静默的时区 bug。**不推荐**——这个项目在这上面的败绩已经够多了。

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
   在两边是同一个时刻（不是差 8 小时、也不是丢了 tzinfo）
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
