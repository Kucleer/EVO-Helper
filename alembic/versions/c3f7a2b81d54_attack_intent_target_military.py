"""攻击意图记下「当时看到的目标军力」

Revision ID: c3f7a2b81d54
Revises: b1d9e47f2a03
Create Date: 2026-08-18

攻击日志上的第二个问题是「当时凭什么打它」。答案本来只存在于 `bot_targets`
那一行的当前值里，而那一行**每采一次军力榜就被整行覆盖**——生产实测
（2026-08-18）同一批目标一天之内从 31,756 刷到 2,616。事后拿现值去 join，
显示的是「现在它多强」，而不是「当时我以为它多强」，这两件事在复盘时恰好相反。

所以在写意图那一刻把读数**快照**下来，两列一起：

- `target_military_score`：当时那一行的军力值；
- `target_military_score_at_utc`：那个值是什么时候读到的。

只有分数不够。2026-08-17 一整天的排障反复卡在「这个分数是什么时候读的」——
实机 10:30 打过一个读数已经 24 小时前的目标，而日志上看不出来。

⚠️ **两列都可空、都不给 `server_default`。** 存量意图没有这个快照，NULL 就是
「当时没记」，页面照实显示「—」。给个默认值、或者拿 `bot_targets` 的现值回填
历史行，都是把编造出来的数写成观测记录——而这一列存在的全部意义就是回答
「当时看到的是多少」，一旦掺进现值就再也分不出哪几行是真的。

`0` 同样不能当缺省：军力 0 是一个合法读数（被打空的 bot），与「没读数」是两件事。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f7a2b81d54"
#: 接在 `b1d9e47f2a03`（删掉军力时间池）后面。**保持单一 head**：并排挂在同一个
#: 父节点上会变成两个 head，`alembic upgrade head` 直接报「Multiple head revisions」。
down_revision: str | None = "b1d9e47f2a03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "attack_intents"
_SCORE = "target_military_score"
_OBSERVED_AT = "target_military_score_at_utc"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column(_SCORE, sa.Float(), nullable=True))
        batch.add_column(sa.Column(_OBSERVED_AT, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column(_OBSERVED_AT)
        batch.drop_column(_SCORE)
