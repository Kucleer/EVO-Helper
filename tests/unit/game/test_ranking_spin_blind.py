"""滚轮盲滚：**闭环**（拨完读一次、不够就补拨）、只在每轮末尾等一次、以及两条退路。

这一组盯的全是**静默**故障：走不到目标、冲过头、每格都等、拨完不等滑行——
四种都不会抛，只会让盲滚走的距离不是要求的距离，而采回来的数只是静悄悄少一截。

⚠️ 闭环的收敛策略有两条**反直觉**的规矩，别把它们优化掉：

1. 每一轮的格数按「本趟观测到的**最大**行/格」算，也就是**故意走不到**、
   从下方逼近目标（`test_the_notch_count_never_uses_the_average_or_slowest_rate`）。
2. **剩余不足 `SPIN_FINAL_APPROACH_ROWS`(30) 行就收手**，哪怕还没进容差
   （`test_the_last_stretch_is_left_to_the_detection_stage`）。最后那一轮是唯一会
   冲过头的地方——2026-08-22 实机请求 200 行走了 218 行。

两条同一个道理：冲过头不可逆（这一段不读内容，没人会发现榜首被跳过了），
少走一点则由检测段接手（实测约 4.6 秒/屏）。

⚠️ 位置由注入的 `read_position` 直接给出「现在在第几名」，**不再从 `read_rows`
交出来的行里取名次中位数**：那条路逐行裁剪，而滚轮把列表停在非整行位置——
实机上第一轮拨完就读不出名次，闭环当场失效。真的读数器在
`tools.ranking_scan.position_from_image`（整列一次读完，与行对齐无关）。
"""

import math
from dataclasses import dataclass, field

import pytest

from evo_helper.game.ranking_nav import RankingNavigator, SpinResult
from evo_helper.game.ranking_ui import (
    GLIDE_SETTLE_S,
    MAX_SPIN_ROUNDS,
    ROWS_PER_NOTCH,
    ROWS_PER_NOTCH_MAX,
    SCROLL_FROM_Y,
    SCROLL_X,
    SPIN_FINAL_APPROACH_ROWS,
    SPIN_MARK_MIN_ROWS,
    SPIN_TOLERANCE_ROWS,
    WHEEL_GAP_S,
)


@dataclass
class FakeDriver:
    """`RankingDriver` 的假替身。滚轮只记格数——**驱动面上没有「拨 N 格」**。"""

    notches: int = 0
    waits: list[float] = field(default_factory=list)
    clicks: list[tuple[int, int]] = field(default_factory=list)
    presses: int = 0
    moves: list[tuple[int, int]] = field(default_factory=list)
    first_notch_after_move: bool = False

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.clicks.append((x, y))

    def press(self, x: int, y: int, *, label: str = "") -> None:
        self.presses += 1

    def move_to(self, x: int, y: int) -> None: ...

    def hover(self, x: int, y: int) -> None:
        self.moves.append((x, y))

    def release(self) -> None: ...

    def wait(self, seconds: float) -> None:
        self.waits.append(seconds)

    def wheel_notch(self) -> None:
        if self.notches == 0 and self.moves:
            self.first_notch_after_move = True
        self.notches += 1


@dataclass
class SpinningList:
    """一份**会动**的假榜单：拨了多少格就按当轮速率往前走多少名。

    `rates` 是每一轮的「真实行/格」，用完就一直用最后一个。取值都落在
    2026-08-22 实机量到的区间里（0.49–1.25）——这一组用例要模拟的就是那份散布。

    ⚠️ 它把驱动和读屏绑在一起（拨格改的是同一个 `rank`），因为闭环要验的正是
    「拨 → 读 → 再拨」这条回路，而不是某一步各自的行为。
    """

    driver: FakeDriver
    rates: list[float]
    rank: int = 100
    #: 整列一次读时抓到几个 `[N]`；不够 `SPIN_MARK_MIN_ROWS` 就等于读不出。
    #: （真读数器是 `tools.ranking_scan.position_from_image`，那道闸在它里面。）
    readable: int = 13
    #: 拨完第几轮之后就读不出名次（None = 一直读得出）。
    #: 实机 2026-08-22：滚轮停在非整行位置、榜首那几行又是奖章图标，抓得到几个
    #: `[N]` 时灵时不灵。
    blind_after: int | None = None
    #: 每一轮实际走了多少名（给用例断言用）。
    moved: list[int] = field(default_factory=list)
    #: 每一轮发了多少格。取整之后从 `moved` 反推不回来，所以另记一份。
    sent: list[int] = field(default_factory=list)
    _seen_notches: int = 0

    def position(self) -> int | None:
        """把「上一轮拨了多少格」兑现成名次的推进，再答「现在在第几名」。

        站在真读数器的位置上：抓够 `SPIN_MARK_MIN_ROWS` 个 `[N]` 才给数，
        否则如实交 `None`（**不是 0**——「不知道」和「在第 0 名」是两回事）。
        """
        sent = self.driver.notches - self._seen_notches
        if sent:
            rate = self.rates[min(len(self.sent), len(self.rates) - 1)]
            step = round(sent * rate)
            self.rank += step
            self.moved.append(step)
            self.sent.append(sent)
            self._seen_notches = self.driver.notches
        readable = self.readable
        if self.blind_after is not None and len(self.sent) >= self.blind_after:
            readable = 0
        return self.rank if readable >= SPIN_MARK_MIN_ROWS else None


def _nav(driver: FakeDriver) -> RankingNavigator[object]:
    """一个**读不出名次**的导航器：走的是开环退路那一支。"""
    return RankingNavigator(
        driver=driver,
        read_labels=lambda: [],
        read_rows=lambda: [],
        row_has_score=lambda row: True,
        read_position=lambda: None,
        say=lambda _m: None,
    )


def _closed_loop(
    rates: list[float], *, readable: int = 13, said: list[str] | None = None
) -> tuple[RankingNavigator[object], FakeDriver, SpinningList]:
    """一个**读得出名次**的导航器 + 会动的假榜单。

    ⚠️ `read_rows` 故意留空：闭环**一行都不该读**。它要是又走回逐行裁剪那条路，
    这里就会静默地退化成实机上失效的那一版。
    """
    driver = FakeDriver()
    board = SpinningList(driver=driver, rates=rates, readable=readable)
    nav: RankingNavigator[object] = RankingNavigator(
        driver=driver,
        read_labels=lambda: [],
        read_rows=lambda: [],
        row_has_score=lambda row: True,
        read_position=board.position,
        say=(said.append if said is not None else (lambda _m: None)),
    )
    return nav, driver, board


# -- 闭环：拨完读一次，不够就补拨 ----------------------------------------------


def test_a_board_at_the_fastest_known_rate_lands_in_one_round() -> None:
    """跑在**已知最快**速率上的一趟，一轮就该到位、而且不许冲过头。

    格数按 `ROWS_PER_NOTCH_MAX` 算，所以只有真实速率**恰好等于**那个上界时
    才会一轮到位；比它慢就会少走、下一轮补——那正是安全的一侧。
    """
    nav, driver, board = _closed_loop([ROWS_PER_NOTCH_MAX])

    result = nav.spin_blind(rows=200)

    assert result.rounds == 1
    assert board.sent == [math.ceil(200 / ROWS_PER_NOTCH_MAX)]
    assert driver.notches == board.sent[0], "到位之后又拨了一轮"
    assert result.rows_measured is not None
    assert result.rows_measured <= 200 + SPIN_TOLERANCE_ROWS, "冲过头了"


def test_a_short_first_round_is_topped_up_until_the_target_is_reached() -> None:
    """⚠️ **这就是这次改动的全部理由。**

    第一轮真实速率只有 0.55 行/格（实机量到过 0.49），开环那套会就此收工，
    「盲滚 700 行」实走 350 行左右——静默地少走一半。闭环会把差额补回来。
    """
    nav, _driver, board = _closed_loop([0.55, 0.5, 0.5])

    result = nav.spin_blind(rows=700)

    assert result.rounds >= 2, "第一轮明明没走够，却没有补拨"
    assert result.rows_measured is not None
    assert result.rows_measured > 350 * 1.5, "补拨几乎没起作用"
    assert board.rank == 100 + result.rows_measured

    # ⚠️ **慢速板收敛不完，这是有意的。** 格数按上界 1.25 算而真实只有 0.5，
    # 每轮只能走掉剩余的四成，6 轮之后仍差几十行——**而那是安全的一侧**：
    # 少走的部分由检测段逐屏接手，多走的部分没有任何东西接得住。
    assert result.rows_measured <= 700 + SPIN_TOLERANCE_ROWS, "冲过头了"


def test_the_notch_count_never_uses_the_average_or_slowest_rate() -> None:
    """⚠️ **格数按本趟观测到的最大行/格算，不许用平均或最小。**

    造一趟「第一轮 0.9、第二轮 0.5」的，到第三轮时手里有两个观测值，
    三种选法给出三个差很远的格数——而按平均或最小算都会算出**更多**的格数，
    也就是冲过头，而冲过头是不可逆的（这一段不读内容，没人会发现榜首被跳过了）。
    """
    nav, _driver, board = _closed_loop([0.9, 0.5, 0.5])

    nav.spin_blind(rows=1000)

    assert len(board.sent) >= 3, "没走到第三轮，这条用例就验不到「用哪个速率」"
    observed = [moved / sent for moved, sent in zip(board.moved[:2], board.sent[:2], strict=True)]
    remaining = 1000 - sum(board.moved[:2])

    # ⚠️ 除数取「本趟观测」与「已知最快 1.25」的**较大者**。
    # 只取本趟观测是不够的：这一趟观测到的全是 0.9 / 0.5 这种慢的，
    # 拿它们当除数会算出**更多**格数，而真实速率一旦跳回 1.25 就冲过头——
    # **越是遇到慢的一轮，下一轮越危险**。
    assert board.sent[2] == math.ceil(remaining / max([*observed, ROWS_PER_NOTCH_MAX]))
    # 方向比数值更要紧：平均、最小、乃至本趟最大，三种都会多发格数。
    average = sum(observed) / len(observed)
    assert board.sent[2] < math.ceil(remaining / average)
    assert board.sent[2] < math.ceil(remaining / min(observed))
    assert board.sent[2] < math.ceil(remaining / max(observed))


def test_the_last_stretch_is_left_to_the_detection_stage() -> None:
    """⚠️ **剩余不足 30 行就收手，哪怕还没进容差 8 行。**

    2026-08-22 实机：请求 200 行**实走 218 行**，18 行的超出全出在最后一轮——
    剩余只剩几行时，一格的粒度加惯性滑行压不住，而那一轮的分母只有几格，
    行/格的读数噪声大到离谱（逐轮 0.85 / 0.98 / **2.82**，最后那个多半是测量
    假象，它还会喂进下一轮的 `rate_hi`）。

    冲过头不可逆：盲滚段不读内容，榜首那批 bot 被跳过去之后没有任何东西会发现。
    而这几十行让给检测段只要约 4 屏、18 秒。

    造一趟第一轮就走到「差 20 行」的：20 在容差 8 与收手线 30 之间，
    所以这一条只有在**两条判据都在**时才绿——只留容差的话会再补一轮。
    """
    said: list[str] = []
    # 1.125 行/格 × ceil(200/1.25)=160 格 = 180 行，离 200 还差 20。
    nav, _driver, board = _closed_loop([1.125], said=said)

    result = nav.spin_blind(rows=200)

    assert result.rounds == 1, "剩下不到 30 行还去补拨——那正是唯一会冲过头的一轮"
    assert board.sent == [math.ceil(200 / ROWS_PER_NOTCH_MAX)]
    assert result.rows_measured == 180
    remaining = 200 - 180
    assert SPIN_TOLERANCE_ROWS < remaining < SPIN_FINAL_APPROACH_ROWS, "这一趟没验到该验的区间"
    assert any("不补拨" in line for line in said), "收手了却一声不吭，日志上看不出少走了一截"


def test_a_round_that_moves_nothing_stops_the_loop() -> None:
    """拨不动时多拨几轮也一样，而每轮要花 `GLIDE_SETTLE_S` 等滑行。

    ⚠️ 停下来还得**说清停在哪、走了多少**：不说的话，「拨不动」和「一切正常」
    在库里长得一模一样（仓库口径：出事时能只靠日志定位）。
    """
    said: list[str] = []
    nav, _driver, _board = _closed_loop([0.0], said=said)

    result = nav.spin_blind(rows=700)

    assert result.rounds == 1, "一行都没走还接着拨"
    assert result.rows_measured == 0
    assert any("没往前走" in line for line in said)


def test_the_round_budget_is_capped() -> None:
    """每一轮都只走一点点（慢到永远追不上）时，也不许无限补拨。"""
    said: list[str] = []
    # 每轮都比上一轮更慢：`rate_hi` 永远追不上，剩余永远吃不完。
    nav, _driver, _board = _closed_loop([0.6, 0.3, 0.15, 0.08, 0.04, 0.02, 0.01], said=said)

    result = nav.spin_blind(rows=5000)

    assert result.rounds == MAX_SPIN_ROUNDS
    assert len(result.rates) == MAX_SPIN_ROUNDS
    assert any("轮" in line for line in said), "顶到轮数上限却一声不吭"


def test_the_measured_rows_are_the_ones_actually_walked() -> None:
    """`rows_measured` 是**量**出来的，不是格数乘标定算出来的。"""
    nav, driver, board = _closed_loop([0.7, 0.7, 0.7, 0.7, 0.7, 0.7])

    result = nav.spin_blind(rows=300)

    assert result.rows_measured == board.rank - 100
    assert result.rows_measured != round(driver.notches * ROWS_PER_NOTCH)


def test_every_round_waits_once_for_the_glide_before_reading() -> None:
    """⚠️ 不等滑行就读 = 在移动中的画面上逐行裁剪，名字横跨两行、名次读不出，
    于是这一轮的测量作废——而作废的表现是「读不出当前位置」，看着像 OCR 坏了。
    """
    nav, driver, _board = _closed_loop([0.55, 0.5])

    result = nav.spin_blind(rows=700)

    assert driver.waits.count(GLIDE_SETTLE_S) == result.rounds
    assert driver.waits[-1] == GLIDE_SETTLE_S, "最后一次等待必须在末尾：紧接着就要读行"


# -- 开环退路：起点读不出来 ----------------------------------------------------


def test_an_unreadable_start_falls_back_to_the_open_loop() -> None:
    """⚠️ **这条退路必须留。**

    整列读一次也不保证抓得到几个 `[N]`（榜首三名是奖章图标，开榜那一屏尤其少）。
    读不出就整趟不干的话，一次 OCR 失灵就能瘫掉整条采集链路。
    """
    said: list[str] = []
    driver = FakeDriver()
    nav = _nav(driver)
    nav.say = said.append

    result = nav.spin_blind(rows=700)

    assert driver.notches == round(700 / ROWS_PER_NOTCH)
    assert result.rounds == 1
    assert result.rates == ()
    assert result.rows_measured is None, "没测出来就得记 None，不许拿格数换算冒充"
    assert any("闭环" in line for line in said), "这一趟没有闭环保护，必须说出来"


def test_too_few_readable_ranks_counts_as_unreadable() -> None:
    """整列只抓到两三个 `[N]` 时，「中位数」就是其中一个——而那几个都可能是
    串出来的高位噪声（实机当场读到过 `[4781]`，那一屏真实名次只到 20）。
    读数器交 `None`，这一层就退回开环。
    """
    nav, driver, _board = _closed_loop([1.0], readable=SPIN_MARK_MIN_ROWS - 1)

    result = nav.spin_blind(rows=700)

    assert result.rows_measured is None
    assert driver.notches == round(700 / ROWS_PER_NOTCH)


# -- 换算、回滚、以及那些一个字都不许改的老规矩 --------------------------------


def test_the_first_round_starts_from_the_calibration() -> None:
    """没有本趟观测时，第一轮按 `ROWS_PER_NOTCH` 猜——它只剩这一个用处了。"""
    driver = FakeDriver()
    result = _nav(driver).spin_blind(rows=108)
    assert driver.notches == round(108 / ROWS_PER_NOTCH)
    assert result == SpinResult(
        rows_requested=108,
        notches=driver.notches,
        spin_seconds=result.spin_seconds,
        rows_measured=None,
        rounds=1,
        rates=(),
    )


def test_a_bigger_request_sends_more_notches() -> None:
    # ⚠️ 这条挡的是「换算写成乘法」——`ROWS_PER_NOTCH` 是 1.08，乘和除只差 8%，
    # 单点断言看不出来，但方向单调性和量级会。
    few = FakeDriver()
    many = FakeDriver()
    _nav(few).spin_blind(rows=100)
    _nav(many).spin_blind(rows=700)
    assert few.notches < many.notches
    assert many.notches == round(700 / ROWS_PER_NOTCH)
    # 每格约推进一行，所以格数该和行数同一个量级，不该差出一个 8.3 倍的「屏」。
    assert 600 < many.notches < 800


def test_zero_rows_sends_nothing_and_reads_nothing() -> None:
    # 0 是最保守的合法取值：「一格都别拨」。这一支也是这次改动的一键回滚——
    # 置 0 就退回纯慢拖，所以它连末尾那次滑行等待、连一次读屏都不该做。
    reads = 0

    def position() -> int | None:
        nonlocal reads
        reads += 1
        return 1

    nav, driver, _board = _closed_loop([1.0])
    nav.read_position = position

    result = nav.spin_blind(rows=0)

    assert driver.notches == 0
    assert result.notches == 0 and result.rounds == 0
    assert driver.waits == []
    assert reads == 0, "0 行是「什么都别做」，读一次位置也是做事（一遍整列 OCR）"


def test_negative_rows_is_rejected() -> None:
    with pytest.raises(ValueError):
        _nav(FakeDriver()).spin_blind(rows=-1)


def test_spin_waits_once_for_the_glide_not_once_per_notch() -> None:
    # ⚠️ 这条是整个改动的要害：每格都等 = 白改（原先 70 屏 × 2 秒的等待就是
    # 要消掉的东西）。所以「长等待」每轮必须恰好出现一次。
    driver = FakeDriver()
    _nav(driver).spin_blind(rows=500)
    assert driver.waits.count(GLIDE_SETTLE_S) == 1
    assert len([w for w in driver.waits if w >= 1.0]) == 1
    # 末尾那一次，不是开头也不是中间——检测段紧接着就要读行。
    assert driver.waits[-1] == GLIDE_SETTLE_S
    assert set(driver.waits[:-1]) == {WHEEL_GAP_S}


def test_every_notch_is_followed_by_the_measured_gap() -> None:
    # 间隔和格数一对一：拉稀了攒不起动量，实测 117ms/格 时 80 格只走 2 行。
    driver = FakeDriver()
    _nav(driver).spin_blind(rows=200)
    assert driver.waits.count(WHEEL_GAP_S) == driver.notches


def test_spin_never_clicks_or_presses() -> None:
    # 盲滚段全程 `allow_actions` 为假，一次点击、一次按下都不该发出去。
    driver = FakeDriver()
    _nav(driver).spin_blind(rows=500)
    assert driver.clicks == []
    assert driver.presses == 0
    # 闭环那一支同样只准挪指针和拨滚轮。
    nav, closed, _board = _closed_loop([0.55, 0.5])
    nav.spin_blind(rows=700)
    assert closed.clicks == [] and closed.presses == 0


def test_spin_seconds_is_measured_not_computed() -> None:
    # 记的是实测用时（好在事后看出 `time.sleep` 粒度把 16ms 撑成了 31ms），
    # 而假驱动的 wait 是空操作，所以这一趟必然远快于「格数 × 16ms」。
    driver = FakeDriver()
    result = _nav(driver).spin_blind(rows=700)
    assert result.spin_seconds >= 0.0
    assert result.spin_seconds < result.notches * WHEEL_GAP_S


def test_spin_parks_the_pointer_inside_the_list_before_sending_any_notch() -> None:
    """⚠️ 2026-08-22 生产事故：滚轮发到了列表外面，榜单一行没动。

    `open_military_ranking` 最后点的是 `MILITARY_TAB`(1084, 212)，而列表从
    `ROW_FIRST_Y`(257) 才开始——指针停在列表上方 45px 的页签条上。浏览器把滚轮
    路由给指针底下的元素，于是几百个事件全喂给了页签条，而日志照样报「盲滚 700 行」。

    钉两件事：**挪了**，而且**挪在第一格之前**（顺序反了等于没挪）。
    """
    driver = FakeDriver()
    _nav(driver).spin_blind(rows=500)
    assert driver.moves, "一次都没挪指针：滚轮会发到上一个动作留下的位置"
    assert driver.moves[0] == (SCROLL_X, SCROLL_FROM_Y)
    assert driver.first_notch_after_move, "第一格发在挪指针之前，等于没挪"


def test_spin_does_not_move_the_pointer_when_there_is_nothing_to_send() -> None:
    # 0 行是「一格都别拨」，那就一个动作都不该发——挪指针也是动作。
    driver = FakeDriver()
    _nav(driver).spin_blind(rows=0)
    assert driver.moves == []


def test_losing_the_position_mid_loop_reports_unknown_not_zero() -> None:
    """⚠️ **位置读丢了要报「不知道」，不能报「走了 0 行」。**

    2026-08-22 实机：请求 500 行，第一轮拨了 400 格之后名次读不出来
    （滚轮把列表停在非整行位置，逐行裁剪就时灵时不灵），而返回值说 `0`。
    那 400 格是真发出去了的，列表几乎肯定走了几百行——「0」是一个**断言**，
    而真相是「不知道」。这个数一路喂进「翻了 N 行到达 bot 区」，
    也就是自标定的输入；拿 0 去喂会把标定往「一格都走不动」的方向拽。
    """
    nav, _driver, board = _closed_loop([1.0])
    board.blind_after = 1  # 第一轮拨完就读不出

    result = nav.spin_blind(rows=500)

    assert result.rows_measured is None, "读不出位置却报了一个具体数字"
    assert result.notches > 0, "这一趟确实拨过格，不是什么都没做"
