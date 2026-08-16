---
issue: 54
agent: root
type: Fixed
date: 2026-08-16
---

把 2026-08-16 切 PostgreSQL 时留在「只在这台机器上成立」状态的几处补进版本管理，
让另一台机器 `git clone` + `uv sync` 之后真的能起来。

## 一、psycopg 只装在 venv 里，没进 `uv.lock`

切库那天驱动是手动 `pip install` 进 `.venv` 的，`pyproject.toml` 没有对应的组。
症状不是「装不上」——是**装好了、跑得好好的，直到下一次 `uv sync`**：uv 会把
不在参数里的包卸掉，而 `.env` 已经指向 PG，控制台下次启动就倒在
`ModuleNotFoundError: psycopg` 上。换台机器则是从第一次 `uv sync` 就没有它。

这跟 `live` 组当初的坑是同一个（见 `pyproject.toml` 里那段注释：pyautogui / pywin32
也曾经只是手动装着）。补 `db = ["psycopg[binary]>=3.2,<4"]` 并 `uv lock`：
`psycopg` / `psycopg-binary` 3.3.4 + `tzdata` 进锁，装出来和现在 venv 里的同版本。

⚠️ 走 `[binary]` 不走源码版：源码版要 libpq 和编译器，Windows 上装不上。
⚠️ 单开一组不进主依赖：CI 和只跑测试的机器用临时 SQLite 库，装它纯属浪费。

## 二、`8c41b9d201ff` 的 SQLite 专有函数（本次一并入库）

`lower(hex(randomblob(16)))` 在 PG 上不存在，改成按方言分流走内置的
`gen_random_uuid()`（PG 13 起自带，不用装 pgcrypto），SQLite 那支原样保留。

⚠️ **空库也过不去**：那是语句**解析**阶段的 `UndefinedFunction`，轮不到
「表是空的、影响 0 行」把它救回来。所以不能指望「新库反正没数据」绕过去。

## 三、`tools/migrate_sqlite_to_pg.py`（本次一并入库）

一次性搬库脚本，只读源库、只写目标库、目标表非空就跳过（重跑安全），
`--verify-only` 一个字都不写只比行数。三处不显然的取舍写在模块 docstring 里：
走 SQLAlchemy 元数据而不裸拷字节（UUID / 布尔 / 时刻三种表示两边都不同，
手写转换错了不报错）、表序取 `metadata.sorted_tables`（手写清单会跟着新表漂，
`docs/postgres-migration.md` 里那张手画的依赖图就已经缺了 5 张表）、
**迁完要 `setval` 校准自增序列**（不校准的话第一次新增才撞主键，那时旧库已经不用了）。

## 四、文档里两处照着做会踩空的地方

- **`EVO_HELPER_DPR` 这个变量不存在。** 真名是 `EVO_HELPER_DEVICE_SCALE_FACTOR`
  （`Settings.device_scale_factor` 加前缀）。写错**不报错**——`SettingsConfigDict`
  是 `extra="ignore"`，配了个不存在的名字会被静静丢掉，而这个值填不对的后果是
  所有 ROI 读空、所有点击偏几个像素，同样不报错。
- **装依赖那行少了 `--extra dev`**，而下一节的自检第一步就是跑 pytest。
  实测 `uv sync --extra vision --extra live --extra db --dry-run`：会卸掉
  pytest / mypy / ruff / httpx 在内的 18 个包。四组齐了之后 dry-run 只剩版本对齐。

顺带把库的拓扑（库在个人机、两台都连它、`var/` 下的文件不跟着走）、
建 PG 库与只监听私有网卡的步骤、以及「连不上 / 连错库」三种症状的分辨写进
`部署到挂机机器.md`；`postgres-migration.md` 标上执行结果，`.env.example`
补 PG 连接串与 `EVO_HELPER_WEB_TOKEN`。

- Configuration: 新增可选依赖组 `db`；`.env.example` 补 `EVO_HELPER_DATABASE_URL`
  的 PG 写法与 `EVO_HELPER_WEB_TOKEN`。**没有新的环境变量**，代码一行未改。
- Database: 无新迁移。`8c41b9d201ff` 改的是补数据那条 UPDATE 的方言分支，
  两边库都已跑过它（PG 上是空表跑的，验的正是它能不能被解析）。
- Verification: `pytest tests -q` 2124 passed；`ruff check` / `ruff format --check`
  / `mypy src` 全绿。PG 侧在工作机上核对：26 张表 18,848 行逐表相等、
  `alembic_version` = `fa1c3d4e5f67` 与代码 head 一致、`attack_dispatches` 最近 5 条的
  UUID / 布尔 / 时刻逐个相等（时刻精确到微秒）、31 个 UUID 列是原生 `uuid`、38 个时刻列是 `timestamptz`、11 个布尔列是真布尔（不是 0/1）、
  7 张整数主键表的序列都已推过当前最大值。
- Safety: 只读校验用的是 `--verify-only`，没有对生产库写过一个字；
  搬库脚本不删源库任何东西，目标表非空即跳过。控制台与实机链路本次一行未动。
- Rollback: `.env` 的 `EVO_HELPER_DATABASE_URL` 改回 `sqlite:///var/evo-helper.db`
  即回到迁移前（切换前的快照留在 `var/evo-helper.before-pg-20260816-2024.db`）。
  `db` 组是可选 extra，不带它 `uv sync` 就当它不存在。

## 还没做的（要在个人机上做，做不了远程）

第七节自检的 4–12 步全部要在那台机器上跑：`uv sync` 四组、确认读的是 PG 而不是
静默回落的空 SQLite、控制台起得来且页面有数据、窗口几何收敛、开机自启、
最后只读实机跑一轮。**这些都过了再放开真派遣。**
