"""给任务真正的身份（id + 名字），并把出发星球与航线数挂到任务上

Revision ID: d2c4b8a71f39
Revises: c1f70b8a26d4
Create Date: 2026-08-13

用户口径（2026-08-13）：

> 之后的任务需要配置一个出发星球（默认主星，也就是第一颗），以及航线数。也就是
> 可能会新增多个同一个类型的任务，比如 2 个 bot 攻击，从主星出发 5 条航线，
> 从 2 号线出发 2 条航线。

追问确认：**航线上限是按星球各一份的**（不是账号共享），**只有 bot 攻击需要多
任务**（海盗、扫描保持单任务）。

这条迁移只动结构与三行既有配置，一行业务数据都不碰。

## 四件事

1. **`mission_tasks.kind` 不再唯一。** 任务的身份从此是 `id`：接口按 id 寻址、
   调度判据按 id 认人、`mission_runs` 按 id 记账。`kind` 降级成「这是哪条链路」
   这一个属性，并单独建索引（原先那道唯一约束顺带提供的索引不能白丢）。
2. **`mission_tasks.name`。** 同类型的多个任务全靠它区分。既有三行填上各自链路
   的现名，空串留给「没起名」——显示层回落到链路标签。
3. **`mission_tasks.origin_*` 与 `fleet_lines`。** 三列坐标 + 一个航线数，
   **全部可空**，NULL 的含义分别是「用全局主星（`EVO_HELPER_ORIGIN`）」与
   「用 `scheduler_config.fleet_line_limit`」。
4. **`mission_runs.task_id`。** 哪一个任务起的这一轮。历史行留 NULL。

## 既有取值一律原样带过去

用户明确要求不改他配好的任何值（优先级、bot 范围、航线数）。所以：

- `enabled` / `priority` / `params_json` / 各个时刻列 / 失败计数 **一个字节都不动**；
- BOT 那一行按要求显式填上主星 `2:137:18` 与**当前的**全局
  `scheduler_config.fleet_line_limit`（读库取值，不写死一个 6）；
- PIRATE 与 SCAN 那两行的出发星球**留 NULL**。给它们钉死一个坐标等于把「换账号
  改 `EVO_HELPER_ORIGIN`」这条路悄悄堵掉——海盗会继续从上一个账号的星球算飞行
  时间与巡航范围，而全程一句警告都没有。

`2:137:18` 在这里是写死的字面量，因为迁移不该去读进程的环境变量（同一个库在两台
机器上会被迁成两副样子）。它等于本仓 `domain.missions.ORIGIN`，也等于实机上
用户的第一颗星球「奥格瑞玛」。

## SQLite 与那道匿名唯一约束

`kind` 上的唯一约束是 `e29d06f8489f` 用 `sa.UniqueConstraint("kind")` 建的，
**没有名字**——SQLite 里没法按名字 `drop_constraint`。所以这里走
`batch_alter_table(copy_from=..., recreate="always")`：`copy_from` 给出的那份表
定义就是重建后的样子，而它里面**故意不含**那道唯一约束，于是重建出来的新表也就
没有它。⚠️ 改动这个 `copy_from` 时请记得：少写一列会把那一列的数据丢掉，
多写一道约束会把它加回来。

`downgrade()` 反着来，并在唯一性真的被用起来之后**拒绝执行**：同一 `kind` 有两行
时回滚就意味着删数据，而那正是用户配出来的东西。宁可报错让人自己决定删哪一行。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2c4b8a71f39"
down_revision: str | None = "c1f70b8a26d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 本次新增的四列 + 名字列。回滚时逐列删掉。
_ADDED = ("name", "origin_galaxy", "origin_system", "origin_position", "fleet_lines")

#: 回滚时重新加上的那道唯一约束的名字。原先那道是匿名的（`sa.UniqueConstraint
#: ("kind")`），SQLite 里按名字删不掉——所以还回去的这道给个名字，
#: 免得下一个人再撞上同一堵墙。语义与原先完全相同。
_UQ_KIND = "uq_mission_tasks_kind"

#: 既有三行各自的名字。与 `web.display.MISSION_LABELS` 同文，但在这里写死一份
#: ——迁移不该 import 应用代码，那些标签哪天改了也不该回头改写历史行。
_NAMES = {
    "PIRATE": "侦查+攻击海盗",
    "BOT": "扫描+攻击 bot",
    "SCAN": "扫描全星系 bot",
}

#: BOT 那一行要填的出发星球。见模块头「既有取值一律原样带过去」。
_MAIN_ORIGIN = (2, 137, 18)


def _mission_tasks(*, with_added: bool) -> sa.Table:
    """`mission_tasks` 的表定义，供 `batch_alter_table(copy_from=...)` 用。

    **逐条写死而不是反射 metadata**：迁移描述的是某一刻的库结构，跟着活的模型走
    会让这一步在将来模型再变时悄悄改变含义。前 11 列是 `e29d06f8489f` 建表时的
    样子（四个时刻列由 `b6e0a4f21c98` 改成带时区）。

    ⚠️ 两个方向都**不含** `kind` 上的唯一约束：`copy_from` 就是重建后的蓝图，
    upgrade 靠它把那道匿名约束去掉，downgrade 靠 `create_unique_constraint`
    显式加回来（那样它才有名字）。少写一列会把那一列的数据丢掉。
    """
    columns = [
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("params_json", sa.Text(), nullable=False),
        sa.Column("round_started_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quota_exhausted_until_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.Column("disabled_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at_utc", sa.DateTime(timezone=True), nullable=False),
    ]
    if with_added:
        columns += [
            sa.Column("name", sa.String(length=60), nullable=False, server_default=""),
            sa.Column("origin_galaxy", sa.Integer(), nullable=True),
            sa.Column("origin_system", sa.Integer(), nullable=True),
            sa.Column("origin_position", sa.Integer(), nullable=True),
            sa.Column("fleet_lines", sa.Integer(), nullable=True),
        ]
    return sa.Table("mission_tasks", sa.MetaData(), *columns, sa.PrimaryKeyConstraint("id"))


def upgrade() -> None:
    # 唯一约束没有名字，所以只能靠「用一份不含它的表定义重建」把它去掉。
    with op.batch_alter_table(
        "mission_tasks", copy_from=_mission_tasks(with_added=False), recreate="always"
    ) as batch:
        batch.add_column(
            sa.Column("name", sa.String(length=60), nullable=False, server_default="")
        )
        batch.add_column(sa.Column("origin_galaxy", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("origin_system", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("origin_position", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("fleet_lines", sa.Integer(), nullable=True))
    op.create_index("ix_mission_tasks_kind", "mission_tasks", ["kind"])

    bind = op.get_bind()
    for kind, name in _NAMES.items():
        bind.execute(
            sa.text("UPDATE mission_tasks SET name = :name WHERE kind = :kind AND name = ''"),
            {"name": name, "kind": kind},
        )
    # 只填 BOT：它是唯一要长出「多个任务、各自出发星球」的链路，而另外两条留 NULL
    # 才能继续跟着 `EVO_HELPER_ORIGIN` 走（见模块头）。
    galaxy, system, position = _MAIN_ORIGIN
    bind.execute(
        sa.text(
            "UPDATE mission_tasks SET origin_galaxy = :galaxy, origin_system = :system, "
            "origin_position = :position WHERE kind = 'BOT'"
        ),
        {"galaxy": galaxy, "system": system, "position": position},
    )
    # 航线数照抄**此刻**的全局值，不写死一个数：用户已经把它调过，而这条迁移
    # 的用意正是「原样带过去」。`scheduler_config` 缺行时留 NULL——NULL 本来就
    # 表示「用全局值」，语义没有变化。
    bind.execute(
        sa.text(
            "UPDATE mission_tasks SET fleet_lines = "
            "(SELECT fleet_line_limit FROM scheduler_config WHERE id = 1) "
            "WHERE kind = 'BOT' "
            "AND EXISTS (SELECT 1 FROM scheduler_config WHERE id = 1)"
        )
    )

    with op.batch_alter_table("mission_runs") as batch:
        batch.add_column(sa.Column("task_id", sa.Integer(), nullable=True))
    op.create_index("ix_mission_runs_task_id", "mission_runs", ["task_id"])


def downgrade() -> None:
    bind = op.get_bind()
    duplicated = bind.execute(
        sa.text(
            "SELECT kind, COUNT(*) AS n FROM mission_tasks GROUP BY kind HAVING COUNT(*) > 1"
        )
    ).all()
    if duplicated:
        listed = "、".join(f"{kind}×{count}" for kind, count in duplicated)
        raise RuntimeError(
            f"mission_tasks 里同一种链路有多行（{listed}），回滚会重新加上 kind 唯一约束，"
            "也就意味着要删掉用户自己配出来的任务。请先自行决定留哪一行再回滚。"
        )

    op.drop_index("ix_mission_runs_task_id", table_name="mission_runs")
    with op.batch_alter_table("mission_runs") as batch:
        batch.drop_column("task_id")

    op.drop_index("ix_mission_tasks_kind", table_name="mission_tasks")
    with op.batch_alter_table(
        "mission_tasks", copy_from=_mission_tasks(with_added=True), recreate="always"
    ) as batch:
        for column in _ADDED:
            batch.drop_column(column)
        batch.create_unique_constraint(_UQ_KIND, ["kind"])
