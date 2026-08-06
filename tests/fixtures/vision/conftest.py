from __future__ import annotations

from pathlib import Path

import pytest

from evo_helper.tools.fixtures import battle_detail_fixture, mail_list_fixture


@pytest.fixture(scope="session")
def synthetic_vision_fixtures(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("vision-fixtures")
    mail_list_fixture(root / "mail-list.png", items=3)
    battle_detail_fixture(root / "battle-detail.png")
    return root
