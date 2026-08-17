"""军力榜的排序与时间窗。

用户口径（2026-08-17）：「军力榜列表增加排序功能，默认时间排序，列表数据范围为
24 小时内的数据」。拆成两件事：

1. 列表能按列排，默认按「更新时间」倒序——最近读到的排最前面。
2. 默认只出最近 24 小时采到的行，但**放得开**（`window=all`）。排障时要看更早的
   数据，把窗焊死等于把历史藏起来。

⚠️ **时刻一律相对当下算。** 判据本身就是「离现在多久」，写死日期的用例会在某个
与它无关的日子集体转红。

⚠️ **页面读的是 `bot_targets`，不是快照表**（详见 `storage.military_rankings` 的
模块头），所以这里全部走真实写入路径 `save_ranking_targets` 造数据。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from evo_helper.domain.models import Coordinate
from evo_helper.domain.records import RankingTarget
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.military_rankings import BoardSort, MilitaryRankingRepository
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.web.app import create_persistent_app

NOW = datetime.now(UTC).replace(microsecond=0)

#: 三行数据，**四种排序键各自给出一个互不相同的顺序**。
#:
#: 这是这一组用例的关键：如果四个键都悄悄排到了同一列上（比如白名单查表退化成
#: 「一律按军力值」），期望序列就对不上了。序列一样的话，用例会一起绿。
A = Coordinate(2, 100, 5)
B = Coordinate(2, 200, 5)
C = Coordinate(3, 50, 5)
BOARD = (
    RankingTarget(A, 30.0, NOW - timedelta(hours=3), military_rank=2),
    RankingTarget(B, 10.0, NOW - timedelta(hours=1), military_rank=3),
    RankingTarget(C, 20.0, NOW - timedelta(hours=2), military_rank=1),
)
#: 每个排序键**升序**时的坐标顺序。降序就是它倒过来。
ASCENDING = {
    "coordinate": ["2:100:5", "2:200:5", "3:50:5"],
    "score": ["2:200:5", "3:50:5", "2:100:5"],
    "rank": ["3:50:5", "2:100:5", "2:200:5"],
    "observed_at": ["2:100:5", "3:50:5", "2:200:5"],
}


def _client(tmp_path: Path) -> tuple[TestClient, SqlAlchemyRepository, MilitaryRankingRepository]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'rankings.db'}")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    client = TestClient(create_persistent_app(factory, local_token="test-token"))
    return client, SqlAlchemyRepository(factory), MilitaryRankingRepository(factory)


def _coordinates(payload: dict[str, object]) -> list[str]:
    rows = payload["rows"]
    assert isinstance(rows, list)
    return [row["coordinate"] for row in rows]


def test_the_board_defaults_to_newest_read_first(tmp_path: Path) -> None:
    """默认排序是**时间倒序**，不是军力值倒序。

    用户口径（2026-08-17）：「默认时间排序」。数据故意让两种排序给出不同的顺序：
    军力值最高的 `2:100:5` 是三行里读得最早的那一条，所以只要默认还挂在军力值上，
    它就会窜到第一行。
    """
    client, repository, _ = _client(tmp_path)
    repository.save_ranking_targets(list(BOARD))

    payload = client.get("/api/military-rankings").json()

    assert _coordinates(payload) == list(reversed(ASCENDING["observed_at"]))


@pytest.mark.parametrize("sort", get_args(BoardSort))
def test_every_sort_key_really_reorders_the_board(tmp_path: Path, sort: str) -> None:
    """四个排序键都得真的落到自己那一列上，两个方向都得算数。

    参数直接取自 `BoardSort` 本身：日后往白名单里加一列，这条用例会自动要求它
    在 `ASCENDING` 里报到，而不是悄悄地无人过问。
    """
    client, repository, _ = _client(tmp_path)
    repository.save_ranking_targets(list(BOARD))

    ascending = client.get("/api/military-rankings", params={"sort": sort, "direction": "asc"})
    descending = client.get("/api/military-rankings", params={"sort": sort, "direction": "desc"})

    assert _coordinates(ascending.json()) == ASCENDING[sort]
    assert _coordinates(descending.json()) == list(reversed(ASCENDING[sort]))


def test_sorting_survives_paging(tmp_path: Path) -> None:
    """排序在 SQL 里做，所以 `offset/limit` 切的是排好序的那一列。

    ⚠️ 这条钉的是「先取全部再在 Python 里排」那种实现：那样翻页会切在数据库返回的
    原始顺序上，第二页拿到的根本不是第二段。
    """
    client, repository, _ = _client(tmp_path)
    repository.save_ranking_targets(list(BOARD))

    params = {"sort": "score", "direction": "asc", "limit": 1}
    pages = [
        _coordinates(client.get("/api/military-rankings", params={**params, "offset": n}).json())
        for n in range(3)
    ]

    assert [page[0] for page in pages] == ASCENDING["score"]


def test_a_row_read_more_than_a_day_ago_is_outside_the_default_window(tmp_path: Path) -> None:
    """默认只出最近 24 小时的行。

    用户口径（2026-08-17）：「列表数据范围为 24 小时内的数据」。25 小时前那条**不许**
    出现——判据落在 `military_score_at_utc`（页面上那个「更新时间」）上。

    `total` 也一并钉住：计数必须和列表是同一个窗，否则页面会显示「命中 1721 条」
    却只列得出窗内那几十行。
    """
    client, repository, _ = _client(tmp_path)
    repository.save_ranking_targets(
        [
            RankingTarget(Coordinate(2, 137, 5), 99.0, NOW - timedelta(minutes=30)),
            RankingTarget(Coordinate(2, 137, 6), 98.0, NOW - timedelta(hours=25)),
        ]
    )

    payload = client.get("/api/military-rankings").json()

    assert _coordinates(payload) == ["2:137:5"]
    assert payload["total"] == 1


def test_widening_the_window_brings_the_old_row_back(tmp_path: Path) -> None:
    """`window=all` 放开时间窗。

    ⚠️ **这条不是锦上添花。** 24 小时的窗是默认值不是牢笼：排障时经常要看更早的
    数据，2026-08-17 晚上就因为看不到历史绕了路。`7d` 一并测，免得中间那档
    只是摆设。
    """
    client, repository, _ = _client(tmp_path)
    old = Coordinate(2, 137, 6)
    repository.save_ranking_targets(
        [
            RankingTarget(Coordinate(2, 137, 5), 99.0, NOW - timedelta(minutes=30)),
            RankingTarget(old, 98.0, NOW - timedelta(hours=25)),
        ]
    )

    for window in ("all", "7d"):
        payload = client.get("/api/military-rankings", params={"window": window}).json()
        assert "2:137:6" in _coordinates(payload), window
        assert payload["total"] == 2, window


def test_an_empty_window_still_says_how_old_the_data_is(tmp_path: Path) -> None:
    """窗里一条都没有，也不能让人以为库空了。

    「命中 0 条」配上「全榜最近一次采集是三天前」，读的人立刻知道该放开窗；只说
    「尚无军力榜数据」就会被当成扫描挂了。所以 `refreshed_at_utc` **不受时间窗
    影响**，而 `window_start_utc` 得把当前边界说出来。
    """
    client, repository, _ = _client(tmp_path)
    stale = NOW - timedelta(days=3)
    repository.save_ranking_targets([RankingTarget(Coordinate(4, 30, 12), 29_590.0, stale)])

    payload = client.get("/api/military-rankings").json()

    assert payload["total"] == 0
    assert datetime.fromisoformat(payload["refreshed_at_utc"]) == stale
    # 走接口时窗口用的是真实时钟（`NOW` 是模块导入那一刻），差的是用例自己跑的
    # 那几秒，所以比的是「离 24 小时前不到一分钟」而不是相等。
    window_start = datetime.fromisoformat(payload["window_start_utc"])
    assert abs(window_start - (NOW - timedelta(hours=24))) < timedelta(minutes=1)


def test_an_unknown_sort_key_is_refused_instead_of_reaching_the_sql(tmp_path: Path) -> None:
    """排序键必须过白名单。

    ⚠️ `ORDER BY` 的列名**没法走绑定参数**，所以「就拼一下」等于把注入口子敞开。
    判据是当场 422，而不是「拼进去之后碰巧没炸」——用一个带分号的键来问，回来的
    必须是拒绝，而且库还得好好的。
    """
    client, repository, _ = _client(tmp_path)
    repository.save_ranking_targets([RankingTarget(Coordinate(2, 137, 5), 99.0, NOW)])

    hostile = client.get(
        "/api/military-rankings",
        params={"sort": "military_score); DROP TABLE bot_targets; --"},
    )

    assert hostile.status_code == 422
    assert client.get("/api/military-rankings").json()["total"] == 1


def test_an_unknown_direction_or_window_is_refused_too(tmp_path: Path) -> None:
    """方向和时间窗同样是枚举，不是随便什么字符串。"""
    client, _, _ = _client(tmp_path)

    assert client.get("/api/military-rankings", params={"direction": "sideways"}).status_code == 422
    assert client.get("/api/military-rankings", params={"window": "forever"}).status_code == 422


def test_the_repository_refuses_an_unknown_sort_key_on_its_own(tmp_path: Path) -> None:
    """接口那层的 `Literal` 挡不住直接调仓储的人，所以仓储自己也要挡。

    不认识就抛错，**不静默回落到默认排序**：回落会让「按名次排」看起来生效了，
    其实一直在按别的列排，而页面上根本看不出来。
    """
    _, _, board = _client(tmp_path)

    with pytest.raises(ValueError, match="unknown board sort key"):
        board.live_board(sort="latest_owner_name")
    with pytest.raises(ValueError, match="unknown board sort direction"):
        board.live_board(direction="sideways")


def test_the_window_boundary_is_the_row_read_time(tmp_path: Path) -> None:
    """窗口边界钉在 `military_score_at_utc` 上，不是入库时刻。

    这两个能差很远：补录、离线导入、重放一份历史 payload，入库都发生在读完之后
    很久。用入库时刻掐窗会把一条三天前读到的数据算成「最近 24 小时」。
    仓储层收 `now_utc` 就是为了让这条判据能钉死，不受用例跑在哪一秒影响。
    """
    _, repository, board = _client(tmp_path)
    repository.save_ranking_targets(
        [RankingTarget(Coordinate(2, 137, 5), 99.0, NOW - timedelta(hours=10))]
    )

    assert board.live_board(window_hours=24.0, now_utc=NOW).total == 1
    assert board.live_board(window_hours=9.0, now_utc=NOW).total == 0
    assert board.live_board(window_hours=None, now_utc=NOW).total == 1


def test_the_page_offers_the_sorting_and_states_its_range(tmp_path: Path) -> None:
    """页面上得看得见——接口做了而页面没接等于没做。

    两件事：每个排序键在表头上都有一格可点的（`data-column`），以及时间范围那个
    下拉框在。范围文案也钉一下：只写「命中 N 条」会被当成库里的全部，而默认只看
    24 小时——这正是这次要修的那句自相矛盾的说明。
    """
    client, _, _ = _client(tmp_path)
    body = client.get("/rankings").text

    for sort in get_args(BoardSort):
        assert f'data-column="{sort}"' in body, sort
    assert '<select id="window" aria-label="时间范围">' in body
    assert '<option value="all">全部数据</option>' in body
    assert "命中 ${data.total} 条" in body
    assert "WINDOW_LABEL[val('window')]" in body
