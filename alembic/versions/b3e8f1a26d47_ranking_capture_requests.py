"""一次性的「下一趟扫描录逐屏原始行」请求：用户点一次，录完即失效

Revision ID: b3e8f1a26d47
Revises: f2c04b8ae153
Create Date: 2026-09-06

## 为什么要建表，而不是加个配置项

要给「换自愈阀的触发条件」攒证据，得有一批**逐屏原始输入**的语料：新旧算法各自
从同一份输入重建历史，才谈得上对照。而语料只能由被观测的那一趟自己录。

请求必须落库，理由有两条：

- **任务参数是可重复使用的。** 写个布尔进去之后每一趟都会录，而一趟约 1,250 行。
- **「子进程结束」不能保证下次不再开。** 它只能保证那一个进程没了。

## ⚠️ 消费时机：起子进程之前，一条原子语句

    UPDATE ranking_capture_requests
       SET consumed_at_utc = :now, consumed_by_run_id = :run_id
     WHERE consumed_at_utc IS NULL

再看 `rowcount`。仓库里已有这个形状的先例（`release_attack_lines`：带 `WHERE`
条件的 `UPDATE` + 读 `rowcount`），不需要 `SELECT ... FOR UPDATE`——那两样全仓
一次都没用过。

**不能绑到 `begin_mission_run`**：它在子进程起来**之后**才跑。子进程起不来时请求
被白白消费掉，用户再点一次即可 —— 这个失败方向比「多录一趟」便宜，也没有崩溃窗口。

## 两个可空列的取值约定

`consumed_at_utc` / `consumed_by_run_id` 都可空、都**不给** `server_default`。
`NULL` = 「还没被消费」，而不是某个具体时刻 —— 给个默认时刻等于把每一行都摆成
「已经录过了」，那样这张表一建出来就是空的语义。同 `a7d3e91c05b2` 那两列的约定。

`consumed_by_run_id` 不是装饰：语料的完整性检查要回答「这批语料是哪次请求采的」，
而只有这一列把两边接上。

## 为什么用 create_table 而不是 batch_alter_table

新建表，两个方向都直接支持。⚠️ 生产是 PostgreSQL、本地跑 SQLite，这里只用了两种
方言都有的 `Uuid` / `DateTime(timezone=True)`，没有方言分岔。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3e8f1a26d47"
down_revision: str | Sequence[str] | None = "f2c04b8ae153"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ranking_capture_requests"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("requested_at_utc", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at_utc", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by_run_id", sa.Uuid(), nullable=True),
    )
    op.create_index(f"ix_{_TABLE}_requested_at_utc", _TABLE, ["requested_at_utc"])
    # 待消费的那些是 claim 语句唯一要找的行，而它们始终是少数。
    op.create_index(f"ix_{_TABLE}_consumed_at_utc", _TABLE, ["consumed_at_utc"])


def downgrade() -> None:
    op.drop_index(f"ix_{_TABLE}_consumed_at_utc", table_name=_TABLE)
    op.drop_index(f"ix_{_TABLE}_requested_at_utc", table_name=_TABLE)
    op.drop_table(_TABLE)
