"""撤掉攻击配置页上那个「盲拖屏数」：删列 blind_scrolls

Revision ID: c7d92f4a1b60
Revises: b3e8f1a26d47
Create Date: 2026-09-07

那个框**存得进、读不出**：`domain.missions.ranking_command` 的参数表里从来没有
`blind_scrolls`，`MissionScheduler._blind_scrolls()` 也从来没有调用点。填进去会存、
会回显、页面上看着像生效了，而实机行为一个字都不变。

它本想当「不改代码就能回滚到慢拖」的杠杆（见 `b8e1c4a72f05` 里那几段），但杠杆的
另一半（库列 → 命令行）始终没接上。**而人会去拨它的时刻，恰好是滚轮那条路已经出事
的时刻** —— 2026-09-07 就发生了一次：盲滚行数被污染的标定样本压到 596，页面上摆着
这个框，填 80、保存成功、回显 80，行为没有任何变化。

⚠️ **慢拖那条老路没有删，仍然可用，只是只从命令行进**：

    python -m evo_helper.tools.ranking_scan --blind-scrolls N

`tools.ranking_scan` 里 `--blind-scrolls` / `scan(blind_scrolls=...)` /
`drag_blind_rows` 一个都没动，优先级也照旧（只给 `--blind-scrolls` 就退回慢拖）。
撤掉的只是「在页面上配它」这条通路。要回滚的人已经在读日志了，敲一条命令行不是负担；
留着那个框，代价是每个看到它的人都可能被绕进去一次。

⚠️ **`DROP COLUMN` 在 SQLite 3.35+ 原生支持**，本仓库测试用的版本够；PostgreSQL
一向支持。所以不走 `batch_alter_table` 那条重建整张表的路 —— 那条路会把这张表上的
约束与默认值重新生成一遍，而这次要动的只有一列。

⚠️ **回退会把列加回来，但加回来的是空列。** 那一列的值本来对行为没有任何影响，
所以「回退之后旧值没了」不构成损失；真要恢复慢拖，走上面那条命令行。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7d92f4a1b60"
#: 接在 `b3e8f1a26d47`（逐屏语料采样请求那张表）后面，也就是当时的 head。
#: **保持单一 head**：并排挂在同一个父节点上会变成两个 head，`alembic upgrade head`
#: 直接报「Multiple head revisions」，而生产的升级机制就是启动时跑它
#: （`web.runtime._upgrade_database`）——症状是用户重启 bat 之后控制台起不来。
down_revision: str | Sequence[str] | None = "b3e8f1a26d47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "military_attack_config"
_COLUMN = "blind_scrolls"


def upgrade() -> None:
    op.drop_column(_TABLE, _COLUMN)


def downgrade() -> None:
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Integer(), nullable=True))
