"""读回收报告邮件的开关（每趟开封预算）

Revision ID: c4e7b2a91d68
Revises: b8d4f6e0a3c2
Create Date: 2026-09-12

用户口径（2026-09-12）：「我希望这是有个开关，可以让我选择读不读回收，
我担心这花费我太多的时间」。

⚠️ **默认必须是关。** 这是加功能不是修 bug —— 开着会让每一趟信箱多花时间
（评估实测：舰队标签里「舰队返回 : 回收报告」约 2:1，开一封 ≈ 8 秒）。
NULL = 关，与本表其余旋钮同一套语义。

⚠️ **开关和预算合一，不另开一列。** 「读不读」和「每趟最多读几封」是同一个
取舍的两端（整段写在模型那一列上），拆成两列只会多出「开着但预算 0」这种
说不清的状态。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e7b2a91d68"
down_revision: str | Sequence[str] | None = "b8d4f6e0a3c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("military_attack_config") as batch:
        batch.add_column(sa.Column("recycle_mail_opens", sa.Integer(), nullable=True))
    # ⚠️ **不补默认值、不给 server_default。** 留 NULL 就是留「关着」，
    # 而写一个非 0 的默认等于替所有人决定从今天起每趟多花几分钟。


def downgrade() -> None:
    with op.batch_alter_table("military_attack_config") as batch:
        batch.drop_column("recycle_mail_opens")
