from __future__ import annotations

from evo_helper.tools.ingest_scan import coordinate_confirmed, is_bot_name, owner_of


class TestCoordinateConfirmation:
    """冒号又细又矮，OCR 会漏读；核对的是「面板显示的是否就是请求的坐标」。"""

    def test_exact_text_matches(self) -> None:
        assert coordinate_confirmed("2:121:1", "坐标 : [2:121:1]")

    def test_dropped_separator_still_matches(self) -> None:
        assert coordinate_confirmed("2:122:9", ":[2122:9]")

    def test_all_separators_dropped_still_matches(self) -> None:
        assert coordinate_confirmed("2:122:10", "[212210]")

    def test_a_different_coordinate_is_rejected(self) -> None:
        assert not coordinate_confirmed("2:121:1", "坐标 : [2:121:2]")

    def test_a_different_system_is_rejected(self) -> None:
        assert not coordinate_confirmed("2:121:1", "坐标 : [2:131:1]")

    def test_empty_text_is_rejected(self) -> None:
        assert not coordinate_confirmed("2:121:1", "")

    def test_unreadable_text_is_rejected(self) -> None:
        assert not coordinate_confirmed("2:121:1", "坐标 : [e:???]")


class TestOwnerNaming:
    def test_bot_prefix_is_detected(self) -> None:
        assert is_bot_name("bot_2_149_17")

    def test_a_player_is_not_a_bot(self) -> None:
        assert not is_bot_name("LilGriffith")

    def test_missing_name_is_not_a_bot(self) -> None:
        assert not is_bot_name(None)

    def test_placeholder_planets_have_no_owner(self) -> None:
        """「荒芜行星」是系统占位，不是玩家名。"""
        assert owner_of("荒芜行星") is None
        assert owner_of("") is None
        assert owner_of(None) is None

    def test_a_real_owner_is_kept(self) -> None:
        assert owner_of(" LilGriffith ") == "LilGriffith"

    def test_hostile_pirates_are_recorded_as_a_name(self) -> None:
        """敌对海盗有归属方，只是不是玩家；保留名称以便区分空位。"""
        assert owner_of("敌对海盗") == "敌对海盗"


class TestBotNameNormalisation:
    """bot 名后半段是数字，OCR 在小字号上会读成字母。"""

    def test_l_becomes_one(self) -> None:
        from evo_helper.tools.ingest_scan import normalise_bot_name

        assert normalise_bot_name("bot_2_1l21_7") == "bot_2_1121_7"

    def test_e_becomes_two(self) -> None:
        from evo_helper.tools.ingest_scan import normalise_bot_name

        assert normalise_bot_name("bot_e_124_12") == "bot_2_124_12"

    def test_multiple_confusions(self) -> None:
        from evo_helper.tools.ingest_scan import normalise_bot_name

        assert normalise_bot_name("bot_e_123_1e2") == "bot_2_123_122"

    def test_a_clean_name_is_unchanged(self) -> None:
        from evo_helper.tools.ingest_scan import normalise_bot_name

        assert normalise_bot_name("bot_2_125_14") == "bot_2_125_14"

    def test_the_prefix_is_never_rewritten(self) -> None:
        """bot 判定就靠前缀，改前缀等于改判定结果。"""
        from evo_helper.tools.ingest_scan import normalise_bot_name

        # 'bot_' 里的 o 不能被换成 0。
        assert normalise_bot_name("bot_2_1_1").startswith("bot_")

    def test_a_non_bot_name_is_untouched(self) -> None:
        from evo_helper.tools.ingest_scan import normalise_bot_name

        assert normalise_bot_name("LilGriffith") == "LilGriffith"
        assert normalise_bot_name("敌对海盗") == "敌对海盗"
