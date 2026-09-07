"""撤掉「盲拖屏数」那一列的迁移。

本地测试用 `Base.metadata.create_all` 建表，所以模型和迁移可以静默分叉：一路全绿，
只有真实的库会在启动时炸。这里两边对着比一遍——**删列这一类尤其要比**：模型上把
属性删了、迁移忘了删列，本地一个字都看不出来（`create_all` 建出来的表本来就没有
那一列），而生产那张表会一直带着它。

⚠️ **方言取决于跑在哪**：设了 `EVO_HELPER_TEST_DATABASE_URL`（CI 上就是）时这几条
跑在真 Postgres 上，不设时仍是 SQLite。`DROP COLUMN` 两边都原生支持，所以这条迁移
没走 `batch_alter_table`——这一点由下面 `test_upgrade_is_replayable_after_a_downgrade`
兜着：SQLite 上真跑不动的话它会红。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command
from evo_helper.storage.models import MilitaryAttackConfigRow
from support.database import scratch_database_url

REVISION = "c7d92f4a1b60"
DOWN_REVISION = "b3e8f1a26d47"
TABLE = "military_attack_config"
COLUMN = "blind_scrolls"


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[3]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["database_url"] = database_url
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return scratch_database_url(tmp_path, "drop-blind-scrolls-migration.db")


def _columns(database_url: str) -> set[str]:
    return {column["name"] for column in inspect(create_engine(database_url)).get_columns(TABLE)}


def test_this_revision_is_on_a_single_headed_chain() -> None:
    """链上只有一个 head，而这一条在链上。

    生产靠启动时 `alembic upgrade head` 自升（`web.runtime._upgrade_database`），
    多一个 head 就是用户重启 bat 之后控制台直接起不来——而这件事在合并之前一个字
    都看不出来。

    ⚠️ 这条**不再断言「head 就是我」**：后面又接了新的迁移（`d3b9f27c4a81`，
    给 `battle_reports.reported_at_utc` 加索引），「谁是 head」这句话只该由**最新
    那一条**的用例来说，否则每加一条迁移都要回来改一次这里，而改多了就没人再当真
    （同 `test_bot_target_unreadable_migration.py` 里那一段的理由）。
    """
    script = ScriptDirectory.from_config(_config("sqlite://"))

    assert len(script.get_heads()) == 1
    assert REVISION in {revision.revision for revision in script.walk_revisions()}
    assert script.get_revision(REVISION).down_revision == DOWN_REVISION


def test_the_column_is_gone_after_upgrade(database_url: str) -> None:
    """升完之后那一列不在了。"""
    command.upgrade(_config(database_url), "head")

    assert COLUMN not in _columns(database_url)


def test_the_model_no_longer_declares_it(database_url: str) -> None:
    """⚠️⚠️ **模型和迁移必须同时不认它。**

    这是删列这一类唯一会静默分叉的地方：模型上删了属性、迁移忘了删列（或者反过来），
    本地全绿——`create_all` 建的表按模型来，本来就没那一列。只有生产那张真表会带着
    它，然后在下一次「按模型对齐」的时候变成一个没人解释得清的差异。
    """
    command.upgrade(_config(database_url), "head")

    assert not hasattr(MilitaryAttackConfigRow, COLUMN)
    assert COLUMN not in MilitaryAttackConfigRow.__table__.columns


def test_the_other_knobs_survive(database_url: str) -> None:
    """⚠️ **同一张表上其余旋钮一个都不许掉。**

    这条迁移刻意**不走** `batch_alter_table`（那条路会把整张表重建一遍）。真要是
    哪天改成了批量重建而漏抄了一列，症状是「某个旋钮突然回到默认值」——页面上看着
    正常，只是它填的数不见了。所以把邻居们点个名。
    """
    command.upgrade(_config(database_url), "head")

    columns = _columns(database_url)
    for name in (
        "tiers_json",
        "blind_scroll_rows",
        "report_scan_hours",
        "unknown_line_hold_minutes",
        "reconcile_cooldown_minutes",
        "bot_revisit_hours",
        "protection_exclusion_hours",
        "unreadable_exclusion_hours",
        "score_max_age_hours",
        "window_floor",
        "account_line_limit",
        "auto_toggle_log_seconds",
    ):
        assert name in columns, name


def test_downgrade_puts_the_column_back(database_url: str) -> None:
    """回退把列加回来（加回来的是空列——那一列的值本来对行为没有任何影响）。

    ⚠️ **回退目标写 `DOWN_REVISION` 而不是 `-1`**：等下一条迁移接上来，`-1` 从 head
    往回只走一步，落在本条之后那一条上，于是这里会为一个和本条无关的原因红
    （`test_ranking_capture_requests_migration.py` 就是这么红过一次的）。
    """
    config = _config(database_url)
    command.upgrade(config, "head")

    command.downgrade(config, DOWN_REVISION)

    assert COLUMN in _columns(database_url)


def test_upgrade_is_replayable_after_a_downgrade(database_url: str) -> None:
    """回退再升一次要能跑通。

    ⚠️ 这一条同时是「`DROP COLUMN` 在这个方言上真的能跑」的证据：SQLite 3.35 以前
    不支持它，那种环境下这里会红，而不是等到生产升级时才炸。
    """
    config = _config(database_url)
    command.upgrade(config, "head")
    command.downgrade(config, DOWN_REVISION)
    command.upgrade(config, "head")

    assert COLUMN not in _columns(database_url)
