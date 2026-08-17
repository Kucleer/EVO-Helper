"""战报截图单独一张表，字节直接存库

Revision ID: b3d7e5a91c04
Revises: a9d5f31c0e77
Create Date: 2026-08-17

新增 `battle_report_screenshots`：读一份战报时截下来的那一屏面板，字节存进库。

**为什么不存路径。** `artifacts` 那张表存的是路径，而实机 runner 跑在另一台
机器上（`E:\\Kucleer_code\\EVO\\EVO-Helper`），人常在另一台机器上开控制台。
存路径等于在控制台上点开一个必然打不开的链接——库是两台机器唯一共享的东西。

**为什么不塞进 `system_log.payload_json`。** 那张表按设计要保持轻（两周几十万
行，主视图是按时刻倒序翻页），往里塞几十 KB 的二进制会让翻页查询连着 blob 一起
扫。分表还保证了攻击日志的列表查询绝不会碰到这些字节。

⚠️ **两处按方言分岔**：

- 时刻列。`b6e0a4f21c98` 把 Postgres 上的业务时刻列统一成了 `TIMESTAMP WITH
  TIME ZONE`（SQLite 上两种写法渲染出来的都是 `DATETIME`）。新列必须跟上，
  否则 `storage.database.UTCDateTime` 写进去的 aware 值会被静默截成 naive。
- 二进制列。`sa.LargeBinary` 自己就按方言渲染成 `BYTEA` / `BLOB`，不用手写。
  这里显式写下来只为说明：**没有长度上限**，一张图约 40 KB，不设 `length`。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3d7e5a91c04"
down_revision: str | None = "a9d5f31c0e77"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp(dialect_name: str) -> sa.DateTime:
    """这条库上该用的时刻类型。见模块头那段 ⚠️。

    方言名走参数而不是在函数里就地读 `op.get_bind()`：那样它只能在一次真正的
    `alembic upgrade` 中间被调用，于是「Postgres 上到底建成了哪一种」这件事
    在没有 Postgres 的机器上一条断言都写不出来。写法照抄 `a9d5f31c0e77`。
    """
    return sa.DateTime(timezone=dialect_name != "sqlite")


def upgrade() -> None:
    dialect_name = op.get_bind().dialect.name
    op.create_table(
        "battle_report_screenshots",
        sa.Column("id", sa.Uuid(), nullable=False),
        # 外键指向 `battle_reports`：攻击日志那一行点得开，靠的就是这条关联。
        sa.Column("report_id", sa.Uuid(), nullable=False),
        sa.Column("captured_at_utc", _timestamp(dialect_name), nullable=False),
        sa.Column("image_format", sa.String(length=8), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("image_bytes", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["report_id"], ["battle_reports.id"]),
        # 一份战报最多一张图。重复读到同一份战报时不该攒出好几张几乎一样的图。
        sa.UniqueConstraint("report_id", name="uq_report_screenshot_report"),
    )
    # 保留期清理按截图时刻扫，所以那一列要有索引；`report_id` 的索引服务于
    # 列表页那个 `EXISTS`（唯一约束在 Postgres 上自带索引，SQLite 上不一定）。
    op.create_index(
        "ix_battle_report_screenshots_captured_at_utc",
        "battle_report_screenshots",
        ["captured_at_utc"],
    )
    op.create_index(
        "ix_battle_report_screenshots_report_id",
        "battle_report_screenshots",
        ["report_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_battle_report_screenshots_report_id", "battle_report_screenshots")
    op.drop_index("ix_battle_report_screenshots_captured_at_utc", "battle_report_screenshots")
    op.drop_table("battle_report_screenshots")
