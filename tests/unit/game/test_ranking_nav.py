"""进军力排行榜并滚动：按文本找「排名」、切「军事评分」、认出滚到底与掉线。

⚠️ 全程不碰游戏：驱动是假的，OCR 读数是喂进来的清单。

断言尽量钉在**真实像素**上，不写成 `ranking_ui.XXX`：拿被守的常量当尺子，
常量被改坏时测试跟着改口，变异那一轮会是绿的（同
`tests/unit/game/test_planet_list.py` 开头那条）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from evo_helper.game.ranking_nav import (
    RankingNavigator,
    RankingNotReached,
    ScrollOutcome,
    merged_labels,
    ranking_label_x,
)

#: 拖完之后的导航条：实机量到的五个标签中心（2026-08-14）。
FIVE = [(830, "太空舱"), (918, "商店"), (998, "联盟"), (1079, "排名"), (1159, "设置")]

#: 没拖之前那一段。没有「排名」，但读得出东西——所以该拖，不该放弃。
LEFT = [(842, "行星"), (920, "舰队"), (999, "太空舱"), (1080, "商店"), (1160, "联盟")]

#: 点「排名」落在图标那一行（y=862），x 来自这一屏读到的那个词框。
RANKING_CLICK = (1079, 862)

#: 「军事评分」与「经济评分」。后者**一次都不许点**：那是全 0 的另一套数据。
MILITARY_TAB = (1084, 212)
ECONOMY_TAB = (838, 212)

#: `pirate_ui.NAV_SCROLL_RIGHT`。这一段导航条是**拖**出来的，点它纹丝不动。
NAV_ARROW = (1204, 862)

#: 面板左上角的 ✕。
CLOSE = (750, 71)

#: 经济榜：bot 全是 0 分、按坐标顺序排（实机 2026-08-14）。
#:
#: **故意只有 3 行**：开面板那一步的判据是「读得出行 = 面板在眼前」，
#: 那道「铺开了没有」的闸只加在**切完页签之后**。这里少于满屏而测试仍然全绿，
#: 就是在钉这件事。
ECONOMY = ("1 [2:1:1] 0", "2 [2:1:2] 0", "3 [2:1:3] 0")


def _row(rank: int, *, scored: bool = True) -> str:
    """假的一行：`名次 名字 分数`。分数解析不出来时末列写 `?`（实机是 `score=None`）。"""
    return f"{rank} Player{rank} {'29.59K' if scored else '?'}"


def _has_score(row: str) -> bool:
    """站在 `domain.ranking` 位置上的替身：末列不是 `?` 就算解析出了分数。"""
    return not row.endswith(" ?")


def _screen(first_rank: int, *, rows: int = 13, scored: bool = True) -> tuple[str, ...]:
    """一屏榜单。

    默认 **13 行**：那不是随手挑的数，是 `ROW_FIRST_Y`(257) / `ROW_LAST_Y`(795) /
    `ROW_PITCH_PX`(44.8) 这三个实测像素算出来的满屏行数，也正是生产 22 条
    「已切到军事榜」日志里 21 条的行数。
    """
    return tuple(_row(first_rank + index, scored=scored) for index in range(rows))


#: 军事榜：有真实分数、按分数降序、坐标是乱的。
MILITARY_1 = _screen(1)
MILITARY_2 = _screen(9)

#: ⚠️ **生产 08-17 17:36:02 那一屏，原样搬过来。**
#:
#: 5 行、头一行是 `RankingRow(rank=None, name='> <A,', score=None, coordinate=None)`
#: ——名字是噪声、分数解析不出来。它当年顺利过了「非空即可」那道闸，
#: 然后盲翻 **101 屏**一无所获，整趟白跑约 7 分钟。
HALF_RENDERED = ("> <A, ?", *_screen(2, rows=4, scored=False))

#: 行数够、却一行分数都解析不出来。半渲染不一定只掉行数，这一支得单独挡。
NO_SCORES = _screen(1, scored=False)

#: 行数不够、但分数读得出来。用来钉「阈值是从每屏行数推的」，不是恰好挡住噪声。
SHORT_BUT_SCORED = _screen(1, rows=5)


class _Game:
    """一台假游戏：导航条能横着拖，榜单能竖着滚，页签能切。"""

    def __init__(
        self,
        *,
        nav: Sequence[Sequence[tuple[int, str]]] = (FIVE,),
        economy: Sequence[Sequence[str]] = (ECONOMY,),
        military: Sequence[Sequence[str]] = (MILITARY_1,),
        tab: str = "economy",
        tab_works: bool = True,
        nav_restores: bool = True,
    ) -> None:
        self.nav = [list(screen) for screen in nav]
        self.nav_at = 0
        self.economy = [list(screen) for screen in economy]
        self.military = [list(screen) for screen in military]
        self.tab = tab
        self.tab_works = tab_works
        self.nav_restores = nav_restores
        self.at = 0

    def labels(self) -> list[tuple[int, str]]:
        return list(self.nav[self.nav_at])

    def rows(self) -> list[str]:
        screens = self.military if self.tab == "military" else self.economy
        if not screens:
            return []
        return list(screens[min(self.at, len(screens) - 1)])

    def click(self, x: int, y: int) -> None:
        if (x, y) == MILITARY_TAB and self.tab_works:
            self.tab = "military"
            self.at = 0
        elif (x, y) == CLOSE:
            # 关掉再开，面板重新停在**经济评分**（实机 2026-08-14）。
            self.tab = "economy"
            self.at = 0

    def dragged(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        (from_x, from_y), (to_x, to_y) = start, end
        if from_y == 862 and to_y == 862:
            step = 1 if to_x < from_x else -1
            if step < 0 and not self.nav_restores:
                return
            self.nav_at = max(0, min(len(self.nav) - 1, self.nav_at + step))
        elif from_x == to_x:
            self.at = min(max(len(self.economy), len(self.military)) - 1, self.at + 1)


class _Driver:
    """记下每一次点击与每一次「按下-移动-松开」。"""

    def __init__(self, game: _Game, *, move_fails_at: int | None = None) -> None:
        self._game = game
        self._move_fails_at = move_fails_at
        self.clicks: list[tuple[int, int, str]] = []
        #: 每一次拖动：(按下点, [每一步落点])。
        self.drags: list[tuple[tuple[int, int], list[tuple[int, int]]]] = []
        self.trace: list[str] = []
        self.moves_total = 0
        self._pressed: tuple[int, int] | None = None
        self._moves: list[tuple[int, int]] = []

    def click(self, x: int, y: int, *, label: str = "") -> None:
        self.trace.append("click")
        self.clicks.append((x, y, label))
        self._game.click(x, y)

    def press(self, x: int, y: int, *, label: str = "") -> None:
        del label
        self.trace.append("press")
        self._pressed = (x, y)
        self._moves = []

    def move_to(self, x: int, y: int) -> None:
        self.trace.append("move")
        self.moves_total += 1
        if self._move_fails_at is not None and self.moves_total == self._move_fails_at:
            raise RuntimeError("急停：鼠标甩到屏幕左上角了")
        self._moves.append((x, y))

    def release(self) -> None:
        self.trace.append("release")
        if self._pressed is None:
            return
        self.drags.append((self._pressed, list(self._moves)))
        if self._moves:
            self._game.dragged(self._pressed, self._moves[-1])
        self._pressed = None
        self._moves = []

    def wait(self, seconds: float) -> None:
        del seconds

    @property
    def points(self) -> list[tuple[int, int]]:
        return [(x, y) for x, y, _label in self.clicks]


def _navigator(
    game: _Game,
    *,
    read_labels: Callable[[], Sequence[tuple[int, str]]] | None = None,
    read_rows: Callable[[], Sequence[str]] | None = None,
    move_fails_at: int | None = None,
) -> tuple[RankingNavigator, _Driver]:
    driver = _Driver(game, move_fails_at=move_fails_at)
    navigator = RankingNavigator(
        driver=driver,  # type: ignore[arg-type]
        read_labels=read_labels or game.labels,
        read_rows=read_rows or game.rows,
        row_has_score=_has_score,
        # 这一份用例全走「开榜 / 滚一屏 / 收尾」，碰不到 `spin_blind` 的闭环
        # （那一段的用例在 `test_ranking_spin_blind.py`）。交 None 就是「读不出」，
        # 也是最保守的取值：真被走到了，闭环会退回开环而不是拿一个假名次接着算。
        read_position=lambda: None,
        say=lambda _message: None,
    )
    return navigator, driver


def _scripted(screens: Sequence[Sequence[str]]) -> Callable[[], list[str]]:
    """按剧本逐次交出一屏；演完了就一直重复最后一屏。

    每走一遍「开面板 → 切军事」正好读两次（开完读一次、切完读一次），
    所以剧本按 `[开面板那屏, 切完那屏, 开面板那屏, 切完那屏, …]` 排。
    """
    remaining = [list(screen) for screen in screens]

    def read() -> list[str]:
        return list(remaining.pop(0) if len(remaining) > 1 else remaining[0])

    return read


class TestFindingTheRankingTabByText:
    def test_it_clicks_the_x_it_read_on_this_screen(self) -> None:
        navigator, driver = _navigator(_Game())

        navigator.open_military_ranking()

        assert RANKING_CLICK in driver.points

    def test_the_click_follows_the_label_when_the_bar_stops_somewhere_else(self) -> None:
        """**不许写死 1079。** 条停的位置差一点，写死就点到「联盟」或「设置」。"""
        shifted = [(x + 17, text) for x, text in FIVE]
        navigator, driver = _navigator(_Game(nav=(shifted,)))

        navigator.open_military_ranking()

        assert (1096, 862) in driver.points
        assert RANKING_CLICK not in driver.points

    def test_a_label_split_into_single_characters_is_merged_before_matching(self) -> None:
        """tesseract 对中文按字切词：不合并的话「排名」永远只读到「排」或「名」。"""
        split = [(830, "太空舱"), (918, "商店"), (998, "联盟"), (1067, "排"), (1091, "名")]
        navigator, driver = _navigator(_Game(nav=(split,)))

        navigator.open_military_ranking()

        assert RANKING_CLICK in driver.points

    def test_two_neighbouring_labels_are_never_merged_into_one(self) -> None:
        """合并阈值放宽到跨标签的字距（≈57px）就会读出 `联盟排名`，于是永远找不到。"""
        split = [(918, "商店"), (986, "联"), (1010, "盟"), (1067, "排"), (1091, "名")]
        navigator, driver = _navigator(_Game(nav=(split,)))

        navigator.open_military_ranking()

        assert RANKING_CLICK in driver.points

    def test_a_label_with_one_misread_character_still_snaps_back(self) -> None:
        """实机上 `chi_sim` 把「攻击」读成过「政击」。差一个字就漏掉是不行的。"""
        navigator, driver = _navigator(_Game(nav=([(1079, "排若")],)))

        navigator.open_military_ranking()

        assert RANKING_CLICK in driver.points

    def test_a_reading_two_characters_away_is_not_trusted(self) -> None:
        """容差放到 2 的话 `排若若` 会被唯一地贴成「排名」——那已经不是「读错一个字」了。

        吸附的意义在于「读错一个字仍认得出」，不是「随便挑一个最像的」。
        """
        navigator, driver = _navigator(_Game(nav=([(1079, "排若若")],)))

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()
        assert driver.points == []

    def test_an_ambiguous_reading_is_refused_instead_of_guessed(self) -> None:
        """`排店` 离「排名」和「商店」都是 1。两个候选并列时**宁可判不出来也不猜**。"""
        navigator, driver = _navigator(_Game(nav=([(1079, "排店")],)))

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()
        assert driver.points == []

    def test_a_label_read_outside_the_label_row_is_not_clicked(self) -> None:
        """ROI 之外的 x 不当候选。这道闸眼下打不着，留着是因为 ROI 与点击是两件会各自变的事。"""
        navigator, driver = _navigator(_Game(nav=([(1400, "排名")],)))

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()
        assert driver.points == []


class TestDraggingTheNavBar:
    def test_the_bar_is_dragged_until_the_ranking_tab_shows_up(self) -> None:
        navigator, driver = _navigator(_Game(nav=(LEFT, FIVE)))

        navigator.open_military_ranking()

        assert len(driver.drags) == 1
        assert RANKING_CLICK in driver.points

    def test_the_bar_is_not_dragged_when_the_ranking_tab_is_already_there(self) -> None:
        """先认出这一屏再点下一下：已经看得见就别再拖，拖过头点的是别的东西。"""
        navigator, driver = _navigator(_Game(nav=(FIVE,)))

        navigator.open_military_ranking()

        assert driver.drags == []

    def test_the_arrow_at_1204_is_never_clicked(self) -> None:
        """⚠️ 实机先点了 `pirate_ui.NAV_SCROLL_RIGHT`(1204, 862)，导航条**纹丝不动**。

        这一段是拖出来的，不是点箭头翻页的。
        """
        navigator, driver = _navigator(_Game(nav=(LEFT, FIVE)))

        navigator.open_military_ranking()

        assert NAV_ARROW not in driver.points

    def test_the_nav_drag_runs_between_the_two_calibrated_x(self) -> None:
        navigator, driver = _navigator(_Game(nav=(LEFT, FIVE)))

        navigator.open_military_ranking()

        (start, moves) = driver.drags[0]
        assert start == (1122, 862)
        assert moves[-1] == (860, 862)

    def test_it_gives_up_instead_of_dragging_for_ever(self) -> None:
        """一路拖到底也没有「排名」时必须停下来，而且一下都不点。"""
        navigator, driver = _navigator(_Game(nav=(LEFT,)))

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()
        assert len(driver.drags) <= 2
        assert driver.points == []

    def test_an_unreadable_nav_bar_costs_zero_presses_and_zero_clicks(self) -> None:
        """标签一个都读不出来 = 认不出这一屏。在认不出的画面上按下手指，落点没人知道。"""
        navigator, driver = _navigator(_Game(nav=([],)))

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()
        assert driver.drags == []
        assert driver.points == []

    def test_an_empty_first_reading_is_read_again_before_giving_up(self) -> None:
        """**空结果不是证据。** 拖动中有加载动画，那一帧读到的是半屏或全空。"""
        answers: list[list[tuple[int, str]]] = [[], [], list(FIVE)]
        navigator, driver = _navigator(
            _Game(), read_labels=lambda: answers.pop(0) if answers else []
        )

        navigator.open_military_ranking()

        assert RANKING_CLICK in driver.points


class TestConfirmingTheMilitaryBoard:
    def test_the_military_tab_is_clicked_when_the_readback_says_economy(self) -> None:
        navigator, driver = _navigator(_Game(tab="economy"))

        navigator.open_military_ranking()

        assert driver.points.count(MILITARY_TAB) == 1

    def test_the_military_tab_is_clicked_even_when_already_on_it(self) -> None:
        """⚠️ **无条件点一次，不先判断。**

        面板打开时停在**经济评分**（用户 2026-08-14 实测），而页签是**幂等的按钮、
        不是开关**——已经在军事榜上再点一次不会切回去。所以用户的口径是
        「你不用管现在是什么，你需要看什么，就点什么切换」。

        这条同时否掉了原先那版「先认出这一屏，已经在军事榜上就不点」：
        那需要一个「这一屏是不是军事榜」的单屏判据，而**那个判据不存在**
        （见 `tests/unit/domain/test_ranking.py` 里的负面结论）。
        """
        navigator, driver = _navigator(_Game(tab="military"))

        navigator.open_military_ranking()

        assert driver.points.count(MILITARY_TAB) == 1

    def test_the_economy_tab_is_never_clicked(self) -> None:
        """⚠️ 经济榜上 bot 全是 0 分、按坐标顺序排——完全是另一套数据。"""
        navigator, driver = _navigator(_Game(tab="economy"))

        navigator.open_military_ranking()

        assert ECONOMY_TAB not in driver.points

    def test_the_rows_it_returns_are_the_ones_read_after_the_switch(self) -> None:
        """返回的必须是**切完之后**回读到的那一屏，不是切之前那份经济数据。"""
        navigator, _driver = _navigator(_Game(tab="economy"))

        assert navigator.open_military_ranking() == MILITARY_1

    def test_it_refuses_when_the_board_goes_blank_after_the_switch(self) -> None:
        """⚠️ 回读回答的是**另一个**问题。

        它不再判「我在哪个页签上」（那判不出来），而是判「我还在不在排行榜面板上」
        ——点完之后一行都读不出来 = 断线或面板没了，这时候要抛，别接着往下采。
        """
        navigator, driver = _navigator(_Game(tab="economy"), read_rows=lambda: [])

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()
        assert driver.points.count(MILITARY_TAB) <= 1

    def test_a_panel_with_no_readable_row_is_refused_without_touching_the_tab(self) -> None:
        """点开之后一行都读不出来 = 面板没开出来（或者掉线）。这时点页签就是乱点。"""
        navigator, driver = _navigator(_Game(economy=()))

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()
        assert MILITARY_TAB not in driver.points

    def test_an_empty_first_row_reading_is_read_again_before_giving_up(self) -> None:
        game = _Game(tab="military")
        answers: list[list[str]] = [[], list(MILITARY_1)]
        navigator, _driver = _navigator(
            game, read_rows=lambda: answers.pop(0) if answers else list(MILITARY_1)
        )

        assert navigator.open_military_ranking() == MILITARY_1


class TestRefusingAPanelThatHasNotFinishedRendering:
    """⚠️ 这一组判的是**「面板铺开了没有」**，不是「我在哪个页签上」。

    后面那个判据**不存在**，2026-08-14 当场证伪过（两个页签的榜首十三行都是真人、
    分数都在 404.17M 量级，非零判据两边都成立）——见
    `tests/unit/domain/test_ranking.py` 里的负面结论。这里的行数与分数只用来回答
    「这一屏渲染完了吗」，两个页签铺开之后都会过这道闸。**别把它误读成又一次
    尝试判页签。**

    起因：生产 08-17 17:36:02 那一趟读到的首屏只有 5 行、头一行是
    `RankingRow(rank=None, name='> <A,', score=None, ...)`。它过了当时那道
    「非空即可」的闸，然后盲翻 101 屏一无所获，白跑约 7 分钟。
    """

    def test_a_full_screen_with_scores_is_let_through(self) -> None:
        """(a) 正常的那一屏——13 行、分数读得出来——照样一次就过，不许被新闸误伤。"""
        navigator, driver = _navigator(_Game(tab="economy"))

        assert navigator.open_military_ranking() == MILITARY_1
        assert driver.points.count(MILITARY_TAB) == 1
        assert CLOSE not in driver.points

    def test_the_five_row_screen_from_the_incident_is_refused(self) -> None:
        """(b) **原样复现 08-17 17:36:02 那一屏**：5 行 + 头一行 `> <A,` + 分数 None。

        它当年是从「非空即可」那道闸底下走过去的。放行的代价不是报个错，
        是**静悄悄盲翻 101 屏**——日志上看着一切正常。
        """
        navigator, _driver = _navigator(_Game(), read_rows=_scripted([ECONOMY, HALF_RENDERED]))

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()

    def test_a_full_screen_where_no_row_yields_a_score_is_refused(self) -> None:
        """(c) 行数够了也不算数：一行分数都解析不出来，那是还没渲染完。

        半渲染的屏不一定只掉行数——行数这一半挡不住这种，所以两半缺一不可。
        """
        navigator, _driver = _navigator(_Game(), read_rows=_scripted([ECONOMY, NO_SCORES]))

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()

    def test_a_short_screen_reopens_the_panel_instead_of_throwing_on_the_spot(self) -> None:
        """(d) **不达标先重开一次，不是当场把整趟扔掉。**

        重开约十几秒，而当场抛掉的是一整趟采集。顺序也钉住：先点 ✕ 关掉，
        再点「排名」重开，最后才第二次点「军事评分」。
        """
        navigator, driver = _navigator(
            _Game(), read_rows=_scripted([ECONOMY, HALF_RENDERED, ECONOMY, MILITARY_1])
        )

        navigator.open_military_ranking()

        assert driver.points == [
            RANKING_CLICK,
            MILITARY_TAB,
            CLOSE,  # ← 先关掉
            RANKING_CLICK,  # ← 再重开
            MILITARY_TAB,  # ← 最后才第二次切页签
        ]

    def test_a_panel_that_renders_on_the_second_try_is_accepted(self) -> None:
        """(e) 重开之后铺开了，就照常交出那一屏——不因为第一次没铺开而记仇。"""
        navigator, _driver = _navigator(
            _Game(), read_rows=_scripted([ECONOMY, HALF_RENDERED, ECONOMY, MILITARY_1])
        )

        assert navigator.open_military_ranking() == MILITARY_1

    def test_a_panel_that_still_does_not_render_after_reopening_is_refused(self) -> None:
        """(f) 重开之后仍不达标，**才**抛。重开一次不是「重开一次然后无条件放行」。"""
        navigator, driver = _navigator(
            _Game(), read_rows=_scripted([ECONOMY, HALF_RENDERED, ECONOMY, HALF_RENDERED])
        )

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()
        assert driver.points.count(MILITARY_TAB) == 2

    def test_the_blank_screen_still_throws_on_the_spot_without_clicking_the_close(self) -> None:
        """读到 **0 行**那一支不重开，仍然当场抛——而且一下都不点。

        差别不是「0 比 5 更严重」，是**知不知道面板在不在眼前**：读出行了就说明
        面板就开在那儿，点它的 ✕(750, 71) 是安全的；一行都读不出来时连这个都不知道，
        那一点落在哪儿没人知道——「在认不出的画面上不许按下手指」是这条链路的硬红线。
        """
        navigator, driver = _navigator(_Game(), read_rows=_scripted([ECONOMY, ()]))

        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()
        assert driver.points.count(MILITARY_TAB) == 1
        assert CLOSE not in driver.points

    def test_the_row_threshold_follows_the_rows_per_screen_constant(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """(g) ⚠️ **阈值是从「每屏行数」推的，不是写死的 13。**

        每屏几行由版面决定：`ROW_FIRST_Y`(257) / `ROW_LAST_Y`(795) /
        `ROW_PITCH_PX`(44.8) → (795−257)/44.8 = 12 个间隔 + 首行 = 13 行。
        版面一改那三个数会跟着改，而写死的闸还按旧版面判——静默失效。

        这里就把「跟着变」直接量一遍：同一屏 5 行（分数读得出来，所以挡它的
        只能是行数这一半），满屏 13 行时挡掉，把满屏改成 5 行之后放行。
        """
        script = [ECONOMY, SHORT_BUT_SCORED, ECONOMY, SHORT_BUT_SCORED]
        navigator, _driver = _navigator(_Game(), read_rows=_scripted(script))
        with pytest.raises(RankingNotReached):
            navigator.open_military_ranking()

        monkeypatch.setattr("evo_helper.game.ranking_nav.ROWS_PER_SCREEN", 5)
        navigator, _driver = _navigator(_Game(), read_rows=_scripted(script))

        assert navigator.open_military_ranking() == SHORT_BUT_SCORED


class TestScrollingDownTheBoard:
    def test_a_drag_that_changes_the_rows_reports_scrolled(self) -> None:
        navigator, _driver = _navigator(_Game(tab="military", military=(MILITARY_1, MILITARY_2)))

        step = navigator.scroll_once()

        assert step.outcome is ScrollOutcome.SCROLLED
        assert step.rows == MILITARY_2

    def test_a_drag_that_changes_nothing_reports_exhausted(self) -> None:
        """滚到底的判据是「拖了一下内容没变」，不是拖固定次数。"""
        navigator, _driver = _navigator(_Game(tab="military", military=(MILITARY_1,)))

        assert navigator.scroll_once().outcome is ScrollOutcome.EXHAUSTED

    def test_rows_that_vanish_after_the_drag_are_off_page_not_exhausted(self) -> None:
        """⚠️ 实机一小时断了三次，其中一次正好断在第 60 名上。

        判成「到底了」就会把半截榜单当成完整榜单收工——而那是一份看不出错的错数据。
        """
        navigator, _driver = _navigator(_Game(tab="military", military=(MILITARY_1, ())))

        assert navigator.scroll_once().outcome is ScrollOutcome.OFF_PAGE

    def test_it_never_presses_when_the_rows_are_already_gone(self) -> None:
        """已经不在榜单页上了就别再拖：那是在对着别的画面按下手指。"""
        navigator, driver = _navigator(_Game(tab="military", military=()))

        assert navigator.scroll_once().outcome is ScrollOutcome.OFF_PAGE
        assert driver.drags == []

    def test_the_scroll_is_a_stepwise_drag_that_lands_on_the_calibrated_pixel(self) -> None:
        """⚠️ 一步到位的 `dragTo` 会被游戏面板**当成点击**——同样的起止点有时滚有时不滚。"""
        navigator, driver = _navigator(_Game(tab="military", military=(MILITARY_1, MILITARY_2)))

        navigator.scroll_once()

        (start, moves) = driver.drags[0]
        assert start == (960, 700)
        assert len(moves) >= 8, "分步移动才让面板收到连续的 mousemove"
        assert moves[-1] == (960, 300)
        assert (
            driver.trace.index("press") < driver.trace.index("move") < driver.trace.index("release")
        )

    def test_the_pointer_is_released_even_when_a_move_blows_up(self) -> None:
        """急停是从移动里抛出来的，那时手指正按着。不松开的话用户接手时整个桌面都在拖。"""
        navigator, driver = _navigator(
            _Game(tab="military", military=(MILITARY_1, MILITARY_2)), move_fails_at=3
        )

        with pytest.raises(RuntimeError):
            navigator.scroll_once()
        assert driver.trace[-1] == "release"


class TestLeavingTheBoard:
    def test_closing_puts_the_nav_bar_back_where_the_other_modules_expect_it(self) -> None:
        """⚠️ 拖之后 `pirate_ui.NAV_PLANET`(840, 862) 上坐着的是「太空舱」（中心 830）。

        那个东西点开是材料仓库、还会把整条导航条盖住。留着不还原就是给下一条链路埋雷。
        """
        navigator, driver = _navigator(_Game(nav=(LEFT, FIVE)))
        navigator.open_military_ranking()

        assert navigator.close() is True
        assert CLOSE in driver.points
        assert driver.drags[-1][0] == (860, 862)
        assert driver.drags[-1][1][-1] == (1122, 862)

    def test_closing_reports_failure_when_the_bar_did_not_move_back(self) -> None:
        """还原不了就如实说，不要报一个自己没验过的 True。"""
        navigator, _driver = _navigator(_Game(nav=(LEFT, FIVE), nav_restores=False))
        navigator.open_military_ranking()

        assert navigator.close() is False


class TestComposingWithTheParsingLayer:
    def test_it_takes_the_rows_the_domain_layer_actually_produces(self) -> None:
        """`read_rows` 交出来的就是 `domain.ranking.RankingRow`——不必为了迁就这一层压成字符串。

        上面所有用例都喂字符串，看着像这一层认得「行」长什么样。它不认得：
        它只做两件事（看空不空、跟上一屏比相等），所以行的类型是参数化的。
        这一条把两层真的接一次，免得类型上早就对不上却要等实机才发现。
        """
        from evo_helper.domain.ranking import RankingRow, coordinate_of

        economy = [
            RankingRow(rank=1, name="bot_2_1_1", score=0.0, coordinate=coordinate_of("bot_2_1_1"))
        ]
        # 满屏 13 行——那道「铺开了没有」的闸要的就是这个（一屏行数由
        # `ROW_FIRST_Y` / `ROW_LAST_Y` / `ROW_PITCH_PX` 推出来）。
        military = [
            RankingRow(
                rank=rank,
                name=f"bot_4_30_{rank}",
                score=29590.0 - rank,
                coordinate=coordinate_of(f"bot_4_30_{rank}"),
            )
            for rank in range(1, 14)
        ]
        game = _Game()
        driver = _Driver(game)
        navigator: RankingNavigator[RankingRow] = RankingNavigator(
            driver=driver,  # type: ignore[arg-type]
            read_labels=game.labels,
            read_rows=lambda: list(military if game.tab == "military" else economy),
            # ⚠️ 实机注入的就是这一句（见 `tools.ranking_scan.scan`）。
            # 上面那些用例喂的是字符串，接不到这条真实的取分数路径。
            row_has_score=lambda row: row.score is not None,
            # 实机注入的是 `tools.ranking_scan.position_from_image`（整列一次读），
            # 而这条用例只走开榜那一段，碰不到闭环。
            read_position=lambda: None,
            say=lambda _message: None,
        )

        assert navigator.open_military_ranking() == tuple(military)
        assert driver.points.count(MILITARY_TAB) == 1


class TestThePureHelpers:
    def test_characters_within_one_label_are_merged_and_centred(self) -> None:
        assert merged_labels([(1067, "排"), (1091, "名")]) == [(1079, "排名")]

    def test_labels_that_are_far_apart_stay_separate(self) -> None:
        assert merged_labels([(998, "联盟"), (1079, "排名")]) == [(998, "联盟"), (1079, "排名")]

    def test_the_ranking_label_is_found_among_the_other_four(self) -> None:
        assert ranking_label_x(FIVE) == 1079

    def test_a_bar_without_the_ranking_label_gives_nothing(self) -> None:
        assert ranking_label_x(LEFT) is None
