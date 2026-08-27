"""永久黑名单：模仿 bot 命名的玩家不进选靶、不进军力榜、军力值也不再更新。

## 事故（生产，2026-08-27）

`4:268:5` 军力 10580、离主星 4:277:15 很近，排序上永远靠前。而他**根本不是 bot**：

用户口径（2026-08-27，逐字）：

    「4:268:5 这个坐标做特殊处理，永久移出军力榜，做黑名单
     1.这个坐标是玩家，他的 ID 是模仿 bot 命名
     2.因为军力差距过大，所以我们无法发起攻击」

代价是 4 系整整一天：那天起了 17 轮，**每一轮都挑中他**，每一轮都在派遣面板上撞一个
我们认不出的弹窗（军力差距过大），整轮作废。07:00 之后一发没派出去。

## ⚠️ 为什么不能沿用现成的那两个排除列

`protection_seen_at_utc` / `unreadable_seen_at_utc` 都是「时刻 + 旋钮」：时刻是事实，
排除多久是策略，到点自动回池。**窗口治不好这件事** —— 等多久他都还是玩家，军力差距
只会越拉越大。所以这一列没有窗口，而这个区别正是本文件每一条用例在守的东西。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session, sessionmaker

from evo_helper.domain.models import Coordinate
from evo_helper.storage import models as orm
from evo_helper.storage.military_rankings import MilitaryRankingRepository
from evo_helper.storage.repository import RankingTarget, SqlAlchemyRepository

NOW = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)
#: 那个模仿 bot 命名的玩家。
PLAYER = Coordinate(4, 268, 5)
#: 一个正经 bot，用来证明闸门只关掉了该关的那一个。
BOT = Coordinate(4, 269, 7)


def _seed(
    repository: SqlAlchemyRepository,
    *coordinates: Coordinate,
    score: float = 10580.0,
    at_utc: datetime = NOW,
) -> None:
    repository.save_ranking_targets(
        [
            RankingTarget(
                coordinate=coordinate,
                military_score=score,
                military_score_at_utc=at_utc,
                military_score_estimated=False,
                military_rank=None,
            )
            for coordinate in coordinates
        ]
    )


def _coords(rows: list[orm.BotTargetRow]) -> set[Coordinate]:
    return {Coordinate(row.galaxy, row.system, row.position) for row in rows}


# -- 拉黑这个动作本身 ------------------------------------------------------------


def test_blacklisting_records_both_the_moment_and_the_reason(
    repository: SqlAlchemyRepository,
) -> None:
    """⚠️ **理由和时刻一样是必须的。**

    拉黑是永久的：三个月后翻到这一行，「他是模仿 bot 命名的玩家」与「那阵子扫描坏了
    误判的」是完全不同的两件事，而**只有一个时刻的话，两者长得一模一样**。没有理由
    就没人敢把它放回来，于是错拉的黑永远拉着。
    """
    _seed(repository, PLAYER)

    assert repository.blacklist_bot_target(PLAYER, reason="玩家，ID 模仿 bot 命名", at_utc=NOW)

    (row,) = repository.blacklisted_bot_targets()
    assert Coordinate(row.galaxy, row.system, row.position) == PLAYER
    assert row.blacklisted_at_utc == NOW
    assert row.blacklist_reason == "玩家，ID 模仿 bot 命名"


def test_blacklisting_twice_keeps_the_first_moment(repository: SqlAlchemyRepository) -> None:
    """⚠️ 再拉一次只改理由，**不刷新时刻**，并交 False。

    时刻回答的是「什么时候认定他不是 bot 的」。每改一次理由就把它推到现在，那段
    「他从哪天起不再被挑中」的历史就没了 —— 而那正是回头核对「4 系是哪天恢复的」
    要看的那个数。
    """
    _seed(repository, PLAYER)
    repository.blacklist_bot_target(PLAYER, reason="初判", at_utc=NOW)

    assert repository.blacklist_bot_target(PLAYER, reason="补充：军力差距过大") is False

    (row,) = repository.blacklisted_bot_targets()
    assert row.blacklisted_at_utc == NOW, "时刻被后来那次刷掉了"
    assert row.blacklist_reason == "补充：军力差距过大"


def test_a_coordinate_that_is_not_in_the_table_is_not_invented(
    repository: SqlAlchemyRepository,
) -> None:
    """⚠️ 库里没这一行时**不新建**，交 False。

    拉黑的前提是这个坐标已经被当成过目标。凭空造一行等于替扫描链路作主：那一行的
    `is_bot` / `source` / 军力值全是编的，而下游分不出编的和扫出来的。
    """
    assert repository.blacklist_bot_target(PLAYER, reason="手滑", at_utc=NOW) is False
    assert repository.blacklisted_bot_targets() == []


# -- 选靶那一头 ------------------------------------------------------------------


def test_a_blacklisted_target_leaves_the_pool(repository: SqlAlchemyRepository) -> None:
    """⚠️⚠️ **本文件的重点。** 拉黑之后选靶再也看不见他。

    `list_bot_targets` 是最深的那一层，上面挂着三条路：选靶、概览页那四格、区域攻击。
    闸装在这里，三条一起关；装在 `_military_candidates` 只盖得住前两条。
    """
    _seed(repository, PLAYER, BOT)
    repository.blacklist_bot_target(PLAYER, reason="玩家", at_utc=NOW)

    assert _coords(repository.list_bot_targets()) == {BOT}


def test_the_blacklist_has_no_window_to_wait_out(repository: SqlAlchemyRepository) -> None:
    """⚠️⚠️ **没有窗口，等多久都不回来。**

    这是它和 `protection_seen_at_utc` / `unreadable_seen_at_utc` 的全部区别，也是
    最容易被下一个人写回去的地方（那两列都到点回池，照着抄就会给这一列配个旋钮）。
    用户口径是「永久」：等多久他都还是玩家，军力差距只会越拉越大。

    构造：把拉黑时刻推到一年前，仍旧不在池子里。
    """
    _seed(repository, PLAYER, BOT)
    repository.blacklist_bot_target(PLAYER, reason="玩家", at_utc=NOW - timedelta(days=365))

    assert _coords(repository.list_bot_targets()) == {BOT}


# -- 军力榜那一头 ----------------------------------------------------------------


def test_a_blacklisted_target_is_off_the_ranking_board(
    session_factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
) -> None:
    """⚠️ 「永久移出军力榜」是用户逐字要的那一条，而榜是**另一条直查 `bot_targets`
    的 SQL** —— 只在选靶那头拦，他照旧挂在榜上。"""
    _seed(repository, PLAYER, BOT)
    repository.blacklist_bot_target(PLAYER, reason="玩家", at_utc=NOW)

    page = MilitaryRankingRepository(session_factory).live_board(now_utc=NOW)

    assert {row.coordinate for row in page.rows} == {BOT}
    assert page.total == 1, "总数没跟着筛，页面会说「命中 2 条」却只列得出 1 行"


def test_the_boards_refresh_time_ignores_blacklisted_rows(
    session_factory: sessionmaker[Session],
    repository: SqlAlchemyRepository,
) -> None:
    """⚠️ 顶部那句「数据更新时间」也要跟着筛。

    拉黑的行军力值**冻结在拉黑那一刻**（下面那条用例钉的），漏掉它，那句话会被这个
    不再更新的行钉在一个和榜上任何一行都对不上的时刻 —— 这里构造成「拉黑那个更新，
    正经 bot 更旧」，不筛就会报出拉黑那一行的时刻。
    """
    _seed(repository, BOT, score=100.0, at_utc=NOW - timedelta(hours=2))
    _seed(repository, PLAYER, at_utc=NOW)
    repository.blacklist_bot_target(PLAYER, reason="玩家", at_utc=NOW)

    page = MilitaryRankingRepository(session_factory).live_board(now_utc=NOW, window_hours=None)

    assert page.refreshed_at_utc == NOW - timedelta(hours=2)


# -- 采集那一头 ------------------------------------------------------------------


def test_a_blacklisted_row_stops_being_updated_by_the_scan(
    repository: SqlAlchemyRepository,
) -> None:
    """⚠️⚠️ **扫描时就丢掉，库里不再更新它**（用户口径 2026-08-27，逐字）。

    只在读侧拦是不够的：军力榜页面直读 `bot_targets`，那一行的军力值照旧会随每趟
    扫描往上涨，看起来像个还在跟的目标。

    代价是这一行的军力值冻结在拉黑那一刻，往后查不到「他现在多少军力」—— 用户当面
    选的就是这一档，理由是那个数对我们已经没有用处了。
    """
    _seed(repository, PLAYER, score=10580.0)
    repository.blacklist_bot_target(PLAYER, reason="玩家", at_utc=NOW)

    _seed(repository, PLAYER, score=99999.0)

    (row,) = repository.blacklisted_bot_targets()
    assert row.military_score == 10580.0, "拉黑之后军力值还在跟着扫描涨"


def test_the_scan_still_updates_everybody_else(repository: SqlAlchemyRepository) -> None:
    """闸门只关掉拉黑的那一行 —— 同一趟扫描里其他坐标照常更新。

    没有这一条，把 `continue` 写成整趟丢掉（或者把守卫写在循环外面）也照样绿。
    """
    _seed(repository, PLAYER, BOT, score=10580.0)
    repository.blacklist_bot_target(PLAYER, reason="玩家", at_utc=NOW)

    _seed(repository, PLAYER, BOT, score=77.0)

    (bot,) = repository.list_bot_targets()
    assert bot.military_score == 77.0


def test_a_brand_new_coordinate_is_still_inserted(repository: SqlAlchemyRepository) -> None:
    """库里还没有的坐标照常插进来 —— 它不可能是拉黑的（没有行就没有标记）。

    这一条钉的是「别把守卫写成 `if target is None or target.blacklisted...: continue`」
    那种手滑：那样写会让军力榜再也收不到任何新目标，而症状要好几天才看得出来。
    """
    _seed(repository, BOT)

    assert _coords(repository.list_bot_targets()) == {BOT}


@pytest.mark.parametrize("reason", ["", "   "])
def test_the_reason_may_not_be_blank(repository: SqlAlchemyRepository, reason: str) -> None:
    """空理由等于没理由 —— 当场拒掉，别让它悄悄存进去。

    见本文件开头：没有理由的黑名单条目**没人敢删**，因为分不出它是真判断还是手滑。
    """
    _seed(repository, PLAYER)

    with pytest.raises(ValueError):
        repository.blacklist_bot_target(PLAYER, reason=reason, at_utc=NOW)
