"""撞见一封「库里已有」时，那句话与那条 `system_log` 必须说清「已有的是哪一行」。

## 实机故障（用户 2026-08-17 报障：「与攻击日志冲突」）

生产日志上只有一句：

    bot #24:480:6 这份战报（17/08/2026 09:05:46）已经在库里；不重复入库

而攻击日志页上同一个坐标还挂着「已派出 / 待战报」（派于 08-15 22:13、预计战报
08-16 06:52）。两条记录看上去互相打脸。

**真相是没有矛盾**：库里那一行 `match_status='UNMATCHED'`、`dispatch_id` 为空，
它是 08-17 另一发攻击的战报，与页面上那一发相隔 35 小时；页面那一发的战报早在
信箱停摆的那 44 小时里过期了（生产库只有这两条记录，各自成立）。

可这句话当时**说不出口**：日志既没说库里那一行是哪一条、也没说它认没认上派遣。
「跳过入库」听上去就像「顺带跳过了一次认领机会」，于是要判断到底有没有丢东西，
只能去连生产库。判据是「出事时能不能只靠库里的日志定位」——那一晚不能。

所以这里钉三档，每一档说的话都不一样：认上了、本来就认着、至今没认上。
读法与去重本身在 `test_pirate_battle_report_ingest.py`，这里只守这句话。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from evo_helper.domain.models import Coordinate
from evo_helper.tools.pirate_loop import rematch_note

TARGET = Coordinate(4, 480, 6)
REPORTED_AT = datetime(2026, 8, 17, 9, 5, 46, tzinfo=UTC)
#: 页面上那一发：08-15 22:13 派出，与上面那份战报相隔 35 小时。
DISPATCHED_AT = datetime(2026, 8, 15, 22, 13, 59, tzinfo=UTC)


@dataclass(frozen=True)
class _Claim:
    """`repository.StoredReportClaim` 的替身，字段一一对应。"""

    report_id: UUID
    match_status: str | None
    dispatch_id: UUID | None
    dispatched_at_utc: datetime | None


class _Repository:
    """只装了这条诊断路径要问的两件事。

    `rematch_after` 表示「重认这一下认上了没有」；认上了就换一份新的认领状态
    交出去，模拟库被改写之后再读一遍。
    """

    def __init__(
        self,
        claims: tuple[_Claim, ...],
        *,
        rematch_after: tuple[_Claim, ...] | None = None,
    ) -> None:
        self._claims = claims
        self._after = rematch_after
        self.reads: list[tuple[Coordinate, datetime]] = []
        self.rematched: list[tuple[Coordinate, datetime]] = []

    def report_claims_at(self, target: Coordinate, reported_at_utc: datetime) -> tuple[_Claim, ...]:
        self.reads.append((target, reported_at_utc))
        if self._after is not None and self.rematched:
            return self._after
        return self._claims

    def rematch_report_at(self, target: Coordinate, reported_at_utc: datetime) -> bool:
        self.rematched.append((target, reported_at_utc))
        return self._after is not None


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict[str, Any]]]:
    """截下 `record_system_log`：这条链路的日志**落库不落文件**，实机在另一台机器上。"""
    entries: list[tuple[str, str, dict[str, Any]]] = []

    def _record(
        level: str,
        source: str,
        message: str,
        *,
        payload: Any = None,
        **_rest: Any,
    ) -> None:
        entries.append((level, message, dict(payload or {})))

    from evo_helper.tools import pirate_loop

    monkeypatch.setattr(pirate_loop, "record_system_log", _record)
    monkeypatch.setattr(pirate_loop, "say", lambda _line: None)
    return entries


def _unclaimed() -> _Claim:
    return _Claim(uuid4(), "UNMATCHED", None, None)


def _claimed() -> _Claim:
    return _Claim(uuid4(), "MATCHED", uuid4(), DISPATCHED_AT)


# -- 三档话 ------------------------------------------------------------------


def test_an_unclaimed_row_is_called_out_as_unclaimed(logged) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **本文件的重点。** 库里那一行没认领任何派遣时，这句话必须明说。

    这正是 4:480:6 那天的形状。少了这一句，用户看到的就是「战报已在库里」与
    「待战报」并排摆着，而两者都没说自己讲的是哪一发。
    """
    repository = _Repository((_unclaimed(),))

    note = rematch_note(repository, TARGET, REPORTED_AT)

    assert "没认领任何派遣" in note
    assert "UNMATCHED" in note
    # 还要点破后果：它的战果不会出现在攻击日志的战果列上——那一格显示「待战报」
    # 并不是这份战报被忽略了，而是它根本不属于那一发。
    assert "攻击日志" in note


def test_a_claimed_row_names_the_leg_it_belongs_to(logged) -> None:  # type: ignore[no-untyped-def]
    """本来就认领着的，要说出**认的是哪一发**（按派出时刻），排障才对得上页面。

    只说一句「已认领」不够：页面上同一个坐标可能有好几发，而要判断的正是
    「日志说的那一份，和我在页面上看着的这一行，是不是同一发」。
    """
    repository = _Repository((_claimed(),))

    note = rematch_note(repository, TARGET, REPORTED_AT)

    assert "08-15 22:13:59 UTC 派出的那一发" in note
    assert "没认领" not in note


def test_a_freshly_rematched_row_says_so(logged) -> None:  # type: ignore[no-untyped-def]
    """补认上了仍然要说——这一档是 2026-08-11 那四发 AAA 的出口，不许被新话盖掉。"""
    repository = _Repository((_unclaimed(),), rematch_after=(_claimed(),))

    note = rematch_note(repository, TARGET, REPORTED_AT)

    assert "刚补认上了" in note
    assert "08-15 22:13:59 UTC 派出的那一发" in note


# -- 跳过入库 ≠ 跳过认领 -----------------------------------------------------


def test_skipping_the_insert_still_attempts_the_claim(logged) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **「已在库里」这条路不许连认领一起跳过。**

    入库与认领绑死的话，任何一次重复扫到都会静默丢掉一次认领机会，而信箱里
    那几行每一趟都在——也就是**每一趟都丢一次**。
    """
    repository = _Repository((_unclaimed(),))

    rematch_note(repository, TARGET, REPORTED_AT)

    assert repository.rematched == [(TARGET, REPORTED_AT)]


def test_the_log_says_the_claim_predated_this_skip(logged) -> None:  # type: ignore[no-untyped-def]
    """日志要能当场排除「是这次跳过害得它没认上」这个猜想。

    所以 `claimed_before_skip` 记的是**跳过之前**就已经认领着的行数：这一下
    跳过并没有改变它，而没有这个数就只能靠猜。
    """
    repository = _Repository((_claimed(),))

    rematch_note(repository, TARGET, REPORTED_AT)

    _level, _message, payload = logged[0]
    assert payload["claimed_before_skip"] == 1


# -- 结构化证据 --------------------------------------------------------------


def test_the_system_log_carries_the_row_that_blocked_the_insert(logged) -> None:  # type: ignore[no-untyped-def]
    """落库的那条日志要带上**被去重挡下的那一行本身**：id、认领状态、认的哪一发。

    实机跑在另一台机器上，`var/logs` 跨机取不到——证据不进 `system_log`
    就等于不存在（口径见 `CLAUDE.md`「新功能必须带够用的日志」）。
    """
    claim = _claimed()
    repository = _Repository((claim,))

    rematch_note(repository, TARGET, REPORTED_AT)

    (level, message, payload) = logged[0]
    assert level == "INFO"
    assert "库里已有" in message and str(TARGET) in message
    (row,) = payload["rows"]
    assert row["report_id"] == str(claim.report_id)
    assert row["match_status"] == "MATCHED"
    assert row["dispatch_id"] == str(claim.dispatch_id)
    assert row["dispatched_at_utc"] == DISPATCHED_AT.isoformat()
    assert payload["rematched"] is False
    # payload 要经得起 JSON 编码：`system_log.payload_json` 存的是 Text。
    assert json.loads(json.dumps(payload))["rows"][0]["match_status"] == "MATCHED"


def test_one_entry_per_report_no_throttling(logged) -> None:  # type: ignore[no-untyped-def]
    """一份战报一条，不限流：这条路每趟最多走一次，不是每 tick 都可能触发的那一类。"""
    repository = _Repository((_unclaimed(),))

    rematch_note(repository, TARGET, REPORTED_AT)
    rematch_note(repository, TARGET, REPORTED_AT)

    assert len(logged) == 2


# -- 不许拖累主路径 ----------------------------------------------------------


def test_a_repository_without_the_query_still_works(logged) -> None:  # type: ignore[no-untyped-def]
    """⚠️ 问不出认领状态时**照旧重认、照旧返回**，一个异常都不许漏出去。

    这是一条纯诊断查询，而它夹在「读完战报」与「决定还要不要往下开封」之间：
    漏出去的异常就是把「战报读不回来」那个故障重新造一遍，只是换了个成因。
    """

    class _Bare:
        def __init__(self) -> None:
            self.rematched: list[tuple[Coordinate, datetime]] = []

        def rematch_report_at(self, target: Coordinate, reported_at_utc: datetime) -> bool:
            self.rematched.append((target, reported_at_utc))
            return False

    repository = _Bare()

    assert rematch_note(repository, TARGET, REPORTED_AT) == ""
    assert repository.rematched == [(TARGET, REPORTED_AT)]


def test_a_failing_query_does_not_escape(logged) -> None:  # type: ignore[no-untyped-def]
    """查询自己炸了也一样：说一句、退回空，绝不打断这一趟。"""

    class _Broken:
        def report_claims_at(self, *_args: Any) -> tuple[_Claim, ...]:
            raise RuntimeError("库连不上")

        def rematch_report_at(self, *_args: Any) -> bool:
            return False

    assert rematch_note(_Broken(), TARGET, REPORTED_AT) == ""
