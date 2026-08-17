"""存储层集成测试共用的库与夹具。

每个测试自己一个临时 SQLite 文件：这批测试要验的正是「进程退出后靠库恢复」，
共用内存库会让状态在测试之间串味，掩盖掉真正要守的行为。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.models import RunInstance, ScanPlan
from evo_helper.storage.repository import SqlAlchemyRepository
from support.database import scratch_database_url


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_database_engine(scratch_database_url(tmp_path, "test.db"))
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return create_session_factory(engine)


@pytest.fixture
def repository(session_factory: sessionmaker[Session]) -> SqlAlchemyRepository:
    return SqlAlchemyRepository(session_factory)


@pytest.fixture
def run_id(session_factory: sessionmaker[Session]) -> UUID:
    """一次可挂攻击意图的运行。

    意图的 ``run_id`` 是指向 ``run_instances`` 的外键，而 SQLite 的外键约束
    在本项目里是开着的，所以不能随手编一个 UUID。
    """
    created = datetime.now(UTC)
    with session_factory() as session:
        plan = ScanPlan(name="scheduler-fixture", created_at_utc=created)
        session.add(plan)
        session.flush()
        run = RunInstance(
            plan_id=plan.id,
            idempotency_key="scheduler-fixture-001",
            state="SCANNING",
            created_at_utc=created,
        )
        session.add(run)
        session.commit()
        return run.id
