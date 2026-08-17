"""控制台上显示的时刻一律 UTC+8，而库里与接口里一律仍是 UTC。

用户口径（2026-08-17）：「页面显示的最后更新时间，均需要显示为 UTC+8」。用户在
UTC+8 生活和操作，而页面上原先混着 UTC 与 UTC+8 两套显示——读一眼就得先想清楚
这一格是哪一套，排障当场就因此绕过路。

这个文件钉的是**两件互相制衡**的事，缺一件另一件就会走样：

1. **页面显示的是 UTC+8，而且写明了时区。** 只断言「表头上有 (UTC+8)」是不够的：
   标注写着 UTC+8、数字仍是 UTC，比不标注更坏——它把一个能看出来的不方便变成了
   一个看不出来的错误。所以每一条都拿同一个样本时刻去比对整串
   `YYYY-MM-DD HH:MM:SS`。
2. **接口返回的仍是 UTC。** 这条防的是有人为了让页面好看，顺手去改序列化。
   `UTCDateTime`（`storage/database.py`）那套判据整个建立在「读出来是 aware 的
   UTC」上，改一处序列化就能把它悄悄拆掉，而页面照样好看。

⚠️ **样本时刻刻意压在日界上**：UTC 2026-08-16 20:30:15 换算成 UTC+8 是
2026-08-17 04:30:15——**连日期都不同**。取一个白天的时刻，任何「只换了小时没换
日期」或者干脆没换的实现都能蒙混过关。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import (
    BattleReport,
    CoordinateScan,
    FleetSnapshotEntry,
    StateEvent,
)
from evo_helper.infrastructure.system_log import SystemLogRecord
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.storage.system_log import SystemLogRepository
from evo_helper.web.app import create_persistent_app
from evo_helper.web.persistent_service import PersistentApplicationService
from support.database import scratch_database_url
from support.runs import seed_run_instance

#: 同一个瞬时的两种写法。差 8 小时，而且跨了一天。
MOMENT_UTC = datetime(2026, 8, 16, 20, 30, 15, tzinfo=UTC)
SHOWN_UTC8 = "2026-08-17 04:30:15"
#: 换算错了（或者压根没换）最可能显示成的那一串。断言它**不在**页面上。
SHOWN_UTC = "2026-08-16 20:30:15"

TARGET = Coordinate(2, 137, 5)
ORIGIN = Coordinate(2, 137, 18)


@pytest.fixture()
def seeded(tmp_path: Path) -> tuple[TestClient, sessionmaker[Session]]:
    """一个库里每一页都有一行、而且那一行的时刻都是 `MOMENT_UTC` 的控制台。"""
    engine = create_database_engine(scratch_database_url(tmp_path, "timezone.db"))
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)

    # 复查请求（诊断页）：`request_revisit` 用注入的 now，正好把时刻钉死。
    service = PersistentApplicationService(factory, now_utc=lambda: MOMENT_UTC)
    service.request_revisit("target", "时区自测", TARGET)

    repository = SqlAlchemyRepository(factory)
    # 状态事件（诊断页第二张表）。
    repository.append_state_event(
        StateEvent(
            aggregate_type="target",
            aggregate_id=uuid4(),
            event="SCANNED",
            occurred_at_utc=MOMENT_UTC,
            after_state="DONE",
        )
    )
    # 坐标扫描（星球列表的「最近扫描」）。
    repository.save_scan(
        CoordinateScan(
            run_id=seed_run_instance(factory),
            coordinate=TARGET,
            scanned_at_utc=MOMENT_UTC,
            owner_name="bot_2_137_5",
            is_bot=True,
            confidence=1.0,
        )
    )
    # 战报（星球详情页的「舰队快照时间线」）。
    repository.append_report(
        BattleReport(
            report_id=uuid4(),
            reported_at_utc=MOMENT_UTC,
            attacker_origin=ORIGIN,
            defender_target=TARGET,
            fleet=(FleetSnapshotEntry(side="defender", ship_type="钛能守卫者", count=6),),
        )
    )
    # 系统日志。
    SystemLogRepository(factory).append(
        [
            SystemLogRecord(
                logged_at_utc=MOMENT_UTC,
                level="INFO",
                source="tests.timezone",
                host="seed-host",
                pid=1,
                message="时区自测",
            )
        ]
    )

    client = TestClient(create_persistent_app(factory, local_token="test-token"))
    client.headers.update({"X-Evo-Helper-Token": "test-token"})
    return client, factory


#: 服务端渲染时刻的那几页：URL、表头上必须出现的时区标注。
#:
#: 表头写死在这里而不是只查 `(UTC+8)` 三个字：把哪一列的标注删掉都得有人转红，
#: 而「页面上某处有 UTC+8」这种断言，删掉任何一列它都还是绿的。
SERVER_RENDERED_PAGES = [
    ("/diagnostics", "请求时间（UTC+8）"),
    ("/diagnostics", "时间（UTC+8）"),
    ("/planets?kind=all", "最近扫描（UTC+8）"),
    ("/targets/2:137:5", "采集时间（UTC+8）"),
    ("/system-log", "时刻（UTC+8）"),
]


class TestPagesShowUtc8:
    @pytest.mark.parametrize(("url", "header"), SERVER_RENDERED_PAGES)
    def test_the_column_says_utc8(
        self, seeded: tuple[TestClient, sessionmaker[Session]], url: str, header: str
    ) -> None:
        client, _ = seeded

        body = client.get(url).text

        assert header in body

    @pytest.mark.parametrize("url", sorted({url for url, _ in SERVER_RENDERED_PAGES}))
    def test_the_moment_itself_is_converted(
        self, seeded: tuple[TestClient, sessionmaker[Session]], url: str
    ) -> None:
        """标注只是标注——真正要钉的是那一格里的数字换算过了。

        `SHOWN_UTC` 那一行是这条断言的全部意义：表头写 UTC+8、格子里摆 UTC，
        是这次改动最容易留下的那种半成品，而它比改动之前更难发现。
        """
        client, _ = seeded

        body = client.get(url).text

        assert SHOWN_UTC8 in body
        assert SHOWN_UTC not in body


class TestOneSharedImplementation:
    """前端那一份换算收在 `base.html`，各页不许再抄。

    抄第二份的那天两处就开始分家，而时区这种东西分家之后没人看得出来——两页
    显示同一个瞬时的两个不同数字，两个都长得像真的。
    """

    def test_the_shared_helper_lives_in_the_base_layout(
        self, seeded: tuple[TestClient, sessionmaker[Session]]
    ) -> None:
        client, _ = seeded

        body = client.get("/rankings").text

        assert "localTime(value)" in body
        assert "Asia/Shanghai" in body

    @pytest.mark.parametrize("url", ["/rankings", "/intel", "/missions"])
    def test_pages_call_the_shared_helper(
        self, seeded: tuple[TestClient, sessionmaker[Session]], url: str
    ) -> None:
        client, _ = seeded

        body = client.get(url).text

        assert "EVOHelper.localTime(" in body

    @pytest.mark.parametrize("url", ["/rankings", "/intel", "/missions"])
    def test_pages_do_not_format_moments_on_their_own(
        self, seeded: tuple[TestClient, sessionmaker[Session]], url: str
    ) -> None:
        """各页不许再自己建日期格式化器，也不许拿 `Date` 走 `toLocaleString()`。

        后者尤其要拦：它跟的是**浏览器**的时区，本机恰好是 UTC+8 所以看着是对的，
        换台机器（或者换个 CI 容器）就换个时刻。

        数字的 `toLocaleString()`（情报中心那一堆舰队数）是另一回事，照样放行——
        所以判据是「同一行里既有 Date 又有 toLocaleString」。
        """
        client, _ = seeded

        # base.html 那一份是共用实现，把它从正文里摘掉再查各页自己写的。
        page = client.get(url).text.split("</head>", 1)[-1]

        offenders = [
            line.strip()
            for line in page.splitlines()
            if "Intl.DateTimeFormat" in line or ("toLocaleString()" in line and "Date" in line)
        ]

        assert offenders == []


class TestApiStaysUtc:
    """⚠️ **接口一律仍是 UTC。**

    这几条防的是「为了显示方便去改序列化」。真那么改的话，上面那些页面断言会
    照样全绿——服务端多减一次 8 小时、前端再加一次 8 小时，页面上正好还是对的，
    而每一个读接口的人（`tools/` 下的脚本、任何存下来的 JSON）都会静默偏 8 小时。
    """

    @pytest.mark.parametrize(
        ("url", "pick"),
        [
            ("/api/revisits", lambda body: body[0]["requested_at_utc"]),
            ("/api/diagnostics/events", lambda body: body[0]["occurred_at_utc"]),
            ("/api/targets/2:137:5/history", lambda body: body[0]["captured_at_utc"]),
            ("/api/system-log", lambda body: body["rows"][0]["logged_at_utc"]),
        ],
    )
    def test_the_field_still_carries_the_utc_instant(
        self,
        seeded: tuple[TestClient, sessionmaker[Session]],
        url: str,
        pick: object,
    ) -> None:
        client, _ = seeded

        body = client.get(url).json()
        raw = pick(body)  # type: ignore[operator]

        moment = datetime.fromisoformat(raw)
        # 既要是同一个瞬时，**也要仍然写成 UTC**：换成 `+08:00` 的写法虽然指的是
        # 同一刻，却已经是改过的序列化格式，下一步就是有人把它当本地时间读。
        assert moment == MOMENT_UTC
        assert moment.tzinfo is not None
        assert moment.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
        assert "04:30:15" not in raw

    def test_the_stored_row_is_untouched(
        self, seeded: tuple[TestClient, sessionmaker[Session]]
    ) -> None:
        """库里那一行也仍是 UTC——显示口径不许倒灌回写入。"""
        _, factory = seeded

        rows = SystemLogRepository(factory).query(limit=10).rows

        assert rows[0].logged_at_utc == MOMENT_UTC
