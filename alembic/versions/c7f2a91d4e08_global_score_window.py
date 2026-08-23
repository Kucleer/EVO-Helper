"""move the military score window (max age + floor) into the global attack config

用户口径（2026-08-23）：「军力攻击的有效期 门限 改为全局设置，不再根据单个星系
进行调整」。这两格从前住在 `mission_tasks.params_json`（键 `score_max_age_hours`
与 `top_n`），任务页 2026-08-22 改版之后一个任务对应一个出发点银河系，于是它们
事实上成了「按星系分别配」。

⚠️ **这条迁移只加两列，一个业务数据都不动。**

存量的 `params_json` 里那两个键**照原样留着**，既不删也不往新列里搬：

- **不往新列搬**：库里有多个军力任务，每个都存着自己那一份。搬哪一份？取最大的、
  最小的、还是 id 最小的那个？三种都是替用户拍一个数，而拍错的症状是所有星系
  一起换了个有效期，页面上却看不出这个数是从哪来的。**这个数该是多少只有用户
  知道**，所以两列都留 NULL = 「跟着代码默认走」（2 小时 / 100 个），让用户去
  攻击配置页填一次。
- **不删旧键**：删了就没法回滚，而且这条迁移在真实库上跑之前无从验证。代码侧
  改成**读到旧键就忽略、并往 `system_log` 落一条 WARNING**（说清这一轮实际用的
  全局值是多少），排障时一眼看得出「配的那个数没生效」。任务页保存一次会把这两个
  旧键从 `params_json` 里清掉。

⚠️ 两列一律**可空、不给 server_default**：NULL 是「跟着代码默认走」这个取值本身。
给了默认值就分不开「没配」和「恰好配成当前默认」，日后调默认值时所有老行都会被
钉死在旧数上。整段先例在 `storage.models.MilitaryAttackConfigRow.blind_scrolls`。

Revision ID: c7f2a91d4e08
Revises: b8e1c4a72f05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7f2a91d4e08"
down_revision: str | Sequence[str] | None = "b8e1c4a72f05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "military_attack_config"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        # 有效期是**浮点**：页面上一直允许 1.5 小时（步长 0.5），存成整数会把用户
        # 配好的值悄悄取整，而取整是静默的。
        batch.add_column(sa.Column("score_max_age_hours", sa.Float(), nullable=True))
        batch.add_column(sa.Column("window_floor", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("window_floor")
        batch.drop_column("score_max_age_hours")
