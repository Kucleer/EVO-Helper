"""面板读数与核对规则。"""

from __future__ import annotations

from evo_helper.vision.scan_reading import (
    COORD_RECIPES,
    FREE_COORD_ROI,
    FREE_NAME_ROI,
    OWNED_COORD_ROI,
    OWNED_PLAYER_ROI,
    PlanetPanel,
    coordinate_confirmed,
    looks_like_mangled_bot,
    normalise_bot_name,
    owner_of,
    read_panel,
    read_panel_confirming,
)


def reader(texts: dict[tuple[int, int, int, int], str]):
    def read(box: tuple[int, int, int, int], *, digits: bool, upscale: int, **_: object) -> str:
        return texts.get(box, "")

    return read


def test_reads_the_owned_layout_and_puts_the_name_in_owner() -> None:
    panel = read_panel(reader({OWNED_COORD_ROI: "[2:121:7]", OWNED_PLAYER_ROI: "bot_2_121_7"}))
    assert panel.layout == "owned"
    assert panel.display_name == "bot_2_121_7"
    assert panel.is_bot


def test_falls_back_to_the_unowned_layout() -> None:
    panel = read_panel(reader({FREE_COORD_ROI: "[2:137:7]", FREE_NAME_ROI: "荒芜行星"}))
    assert panel.layout == "free"
    # 「荒芜行星」是系统占位，不是玩家名。
    assert panel.display_name is None
    assert not panel.is_bot


def test_owned_layout_wins_when_both_boxes_read_something() -> None:
    # 有主布局的名字在 owner；只读 planet_name 会把 bot 当成空位漏掉（踩过）。
    panel = read_panel(
        reader(
            {
                OWNED_COORD_ROI: "2:124:12",
                OWNED_PLAYER_ROI: "bot_2_124_12",
                FREE_NAME_ROI: "荒芜行星",
            }
        )
    )
    assert panel.display_name == "bot_2_124_12"


def test_confirmation_survives_a_dropped_colon() -> None:
    # 坐标的冒号又细又矮，OCR 会整个漏掉；比的是数字序列而不是解析结果。
    assert coordinate_confirmed("2:122:9", "[2122:9]")
    assert PlanetPanel("free", "[2122:9]").confirms("2:122:9")


def test_confirmation_survives_a_bracket_read_as_a_digit() -> None:
    # 实测：有主布局的 [2:12:9] 读成 [2:12:93] / [2:12:39]——那个 3 是右括号。
    # 结构还在，请求串原样出现，所以算数。漏掉它就等于丢掉 bot_2_12_9。
    assert coordinate_confirmed("2:12:9", "[2:12:93]")
    assert coordinate_confirmed("2:12:9", "[2:12:9]")


def test_a_neighbouring_coordinate_is_still_rejected() -> None:
    # 子串判据不能把邻居放进来：银河系是一位数、分隔符位置固定，凑不出请求串。
    assert not coordinate_confirmed("2:12:9", "[2:12:19]")
    assert not coordinate_confirmed("2:1:9", "[2:12:9]")
    assert not coordinate_confirmed("2:12:9", "[2:112:9]")


def test_confirmation_ignores_noise_before_the_bracket() -> None:
    # ROI 会带进「坐标：」的尾巴，数字白名单把它读成 '4:'——实测出现过 '4:[2:6:15]'。
    assert coordinate_confirmed("2:6:15", "4:[2:6:15]")
    assert coordinate_confirmed("2:6:15", ": [2:6:15]")


def test_confirmation_rejects_a_different_coordinate() -> None:
    assert not coordinate_confirmed("2:122:9", "[2:122:8]")
    assert not coordinate_confirmed("2:122:9", "")
    # 括号里少一位仍然不算数——挡掉前缀噪声不等于放松判据。
    assert not coordinate_confirmed("2:3:11", "4:[2:3:1]")
    # 没有括号时按整段比，读到的是上一个坐标就该被拒。
    assert not coordinate_confirmed("2:123:5", ": :123:4")


def test_bot_name_repair_leaves_the_prefix_alone() -> None:
    # 前缀就是 bot 判定本身，改它等于改判定结果。
    assert normalise_bot_name("bot_2_1l21_7") == "bot_2_1121_7"
    assert normalise_bot_name("bot_e_124_12") == "bot_2_124_12"
    assert normalise_bot_name("LilGriffith") == "LilGriffith"


def ladder_reader(by_recipe: dict[tuple[int, str], str]):
    """按配方给不同读数，模拟「头一套读不出粘连的 1、换一套读得出」。"""

    def read(
        box: tuple[int, int, int, int],
        *,
        digits: bool,
        upscale: int,
        resample: str = "lanczos",
        **_: object,
    ) -> str:
        if box == FREE_COORD_ROI:
            return by_recipe.get((upscale, resample), "")
        if box == FREE_NAME_ROI:
            return "bot_2_111_11"
        return ""

    return read


FIRST, SECOND = COORD_RECIPES[0], COORD_RECIPES[1]


def test_the_ladder_recovers_a_coordinate_the_first_recipe_cannot_read() -> None:
    # 实测：LANCZOS 把 2:111:11 里相邻的 1 糊成一根，读出 2:10:11；换最近邻就读对了。
    # 位 11 是全宇宙的 1/16，11x 系是每百个恒星系里的十个——只用一套就等于成批丢坐标。
    panel = read_panel_confirming(ladder_reader({FIRST: "2:10:11", SECOND: "2:111:11"}), "2:111:11")
    assert panel.confirms("2:111:11")
    assert panel.display_name == "bot_2_111_11"


def test_the_ladder_stops_at_the_first_recipe_that_confirms() -> None:
    panel = read_panel_confirming(ladder_reader({FIRST: "2:111:11", SECOND: "垃圾"}), "2:111:11")
    assert panel.coordinate_text == "2:111:11"


def test_the_ladder_reports_the_last_read_when_nothing_confirms() -> None:
    # 都不过就如实返回最后一次读数，交给调用方记缺口——不能假装读到了。
    last = COORD_RECIPES[-1]
    panel = read_panel_confirming(ladder_reader({FIRST: "2:10:11", last: "2:11:11"}), "2:111:11")
    assert not panel.confirms("2:111:11")
    assert panel.coordinate_text == "2:11:11"


def test_owner_of_treats_placeholders_as_empty() -> None:
    assert owner_of("荒芜行星") is None
    assert owner_of("  ") is None
    assert owner_of(" LilGriffith ") == "LilGriffith"


def owner_ladder_reader(by_recipe: dict[tuple[int, str], str]):
    """有主布局；名字行按配方给不同读数。"""

    def read(
        box: tuple[int, int, int, int],
        *,
        digits: bool,
        upscale: int,
        resample: str = "lanczos",
        **_: object,
    ) -> str:
        if box == OWNED_COORD_ROI:
            return "2:9:5"
        if box == OWNED_PLAYER_ROI:
            return by_recipe.get((upscale, resample), by_recipe.get(("first", ""), ""))
        return ""

    return read


def test_a_mangled_bot_prefix_is_reread_instead_of_filed_as_empty() -> None:
    # 实机：bot_2_9_5 读成 botleao.- ——前缀糊了，那颗星球就作为普通空位入库，
    # 而它正是要找的东西。这类失败不报错，靠「每系恰好一个 bot」的分布才发现。
    panel = read_panel_confirming(
        owner_ladder_reader({("first", ""): "botleao.-", (2, "lanczos"): "bot_2_9_5"}),
        "2:9:5",
    )
    assert panel.is_bot
    assert panel.display_name == "bot_2_9_5"


def test_a_name_that_never_reads_as_a_bot_is_left_alone() -> None:
    # 重读读不出 bot_ 就保持原样，绝不硬凑——放宽前缀会把叫 botanist 的真人当成 bot。
    panel = read_panel_confirming(owner_ladder_reader({("first", ""): "botanist"}), "2:9:5")
    assert not panel.is_bot
    assert panel.owner == "botanist"


def test_a_normal_name_is_not_reread() -> None:
    assert not looks_like_mangled_bot("LilGriffith")
    assert not looks_like_mangled_bot("bot_2_9_5")
    assert not looks_like_mangled_bot(None)
    assert looks_like_mangled_bot("botleao.-")
