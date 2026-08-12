"""直接往 `run_instances` 里种一条运行实例。

原先测试是调 `PersistentApplicationService.start_run()` 来拿这条运行的，而
`start_run`（连同 `POST /api/runs/start`）已经删了——那个接口在「运行详情」页
关掉之后就没有任何界面调用方，只剩测试拿它造外键。

**外键本身不能跟着一起省掉。** `attack_intents.run_id` 指向 `run_instances`，
「每一发派遣都挂在一次运行下」是库里的约束；把种子改成不建运行实例（塞 NULL
或者随手编一个 UUID），等于把这条约束从测试里悄悄拿掉，而 SQLite 的外键约束在
本项目里是开着的。所以这里按 `run_instances` 的真实形状插一行，和生产链路
（`tools/scan_coordinates.py`、`tools/pirate_loop.py` 按 `PLAN_NAME` / `RUN_KEY`
落库）写的是同一张表。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import RunState
from evo_helper.storage.models import RunInstance, ScanPlan


def seed_run_instance(
    session_factory: sessionmaker[Session],
    *,
    plan_id: UUID | None = None,
    plan_name: str = "测试计划",
    idempotency_key: str = "test-run-0001",
    state: RunState = RunState.SCANNING,
    created_at_utc: datetime | None = None,
) -> UUID:
    """建一条运行实例并返回它的 id（可直接当 `attack_intents.run_id` 用）。

    `plan_id` 传的是计划的**对外 UUID**（`ScanPlanView.id` / 接口里的 `id`），
    也就是 `create_plan()` 返回的那个；`run_instances.plan_id` 存的是自增主键，
    这里负责换算。不传就现建一个只有名字的计划——多数测试要的只是「有这么一次
    运行」，计划长什么样与被测行为无关。
    """
    created = created_at_utc or datetime.now(UTC)
    with session_factory() as session:
        if plan_id is None:
            plan = ScanPlan(name=plan_name, created_at_utc=created)
            session.add(plan)
            session.flush()
        else:
            found = session.scalar(select(ScanPlan).where(ScanPlan.public_id == plan_id))
            if found is None:  # pragma: no cover - 种子写错了才会走到
                raise LookupError(f"没有 public_id={plan_id} 的扫描计划，先建计划再建运行")
            plan = found
        run = RunInstance(
            id=uuid4(),
            plan_id=plan.id,
            idempotency_key=idempotency_key,
            target_date=datetime(created.year, created.month, created.day, tzinfo=UTC),
            state=state.value,
            created_at_utc=created,
            started_at_utc=created if state is RunState.SCANNING else None,
        )
        session.add(run)
        session.commit()
        return run.id
