"""带 `K` 的舰队数量文本怎么读成艘数。

这个文件原先叫 `test_fleet_tier.py`，守的是「分档」。分档在 2026-08-13 随
「bot 不再分档、一律 BBB」整套删掉了（`domain.fleet_tier` 模块不存在了），
只有解析留了下来，因为它的消费者根本不是分档：`domain.battle_outcome` 算胜负
要的那四个数（我方/敌方的「单位」与「损失单位」）就是靠它读出来的。

所以这里的用例只保留与解析有关的那些，判据也重写成按**新的下游后果**说话：
读错一位不再是「换错一套攻击组合」，而是「记错一条战果」——而战果决定这个坐标
要不要再挨一发（平局才重打，见 `domain.bot_round`）。
"""

from __future__ import annotations

from evo_helper.domain.fleet_counts import parse_fleet_count


def test_plain_numbers() -> None:
    assert parse_fleet_count("517") == 517
    assert parse_fleet_count("2") == 2


def test_the_k_suffix_means_thousands() -> None:
    # 游戏显示 5.36K，真实值在 5355–5364 之间；取 5360。
    assert parse_fleet_count("5.36K") == 5360
    assert parse_fleet_count("1.09K") == 1090
    assert parse_fleet_count("5.73k") == 5730


def test_whitespace_is_tolerated() -> None:
    assert parse_fleet_count("  1.11K ") == 1110


def test_unreadable_text_is_not_guessed() -> None:
    for junk in ("", "K", "abc", "5..3K", "1,090"):
        assert parse_fleet_count(junk) is None


def test_the_m_suffix_is_refused_on_purpose() -> None:
    """`M` 不认。识别侧的白名单本来也只放行 `0123456789.K`
    （`vision.optional.report_screens.UNIT_WHITELIST`），`M` 根本进不来；
    在这里认了它，就等于凭一个从未在实机上见过的后缀记一条战果。
    「没读到」在 `battle_outcome.survivors` 那边的处置是整份拒收，
    这是安全的那一侧。
    """
    for text in ("1.5M", "2M", "8m"):
        assert parse_fleet_count(text) is None


def test_the_k_suffix_needs_its_decimal_point() -> None:
    """实机 2026-08-11 的量级错**不是**在这里发生的。

    2:48:12 的守方单位实为 `1.22K`，这个函数给出 1220（正确）；入库的 122000
    来自 `122K`——小数点在 OCR 那一层就掉了，修在
    `vision.fleet_counts.pick_count`。
    """
    assert parse_fleet_count("1.22K") == 1220
    assert parse_fleet_count("122K") == 122000


def test_a_rounded_reading_barely_moves_the_number() -> None:
    """`5.36K` 读成 `5.35K` 只差 10 艘。

    差 10 艘不会改变 `剩余 = 单位 − 损失` 是不是 0，也就不会改变战果——
    末位误差在这条链路上没有后果，这条断言把那件事钉住。
    """
    assert abs((parse_fleet_count("5.36K") or 0) - (parse_fleet_count("5.35K") or 0)) == 10


def test_a_lost_leading_digit_is_refused_rather_than_read_small() -> None:
    """而丢首位（`5.36K` 读成 `.36K`）差一个数量级——这正是识别要防的那一类错。

    这里**拒收**而不是读成 360：正则要求小数点前面有数字。拒收之后
    `battle_outcome.survivors` 整份不判胜负，而读成 360 会让一条战果凭一次
    OCR 失手被记下来，还可能把一场平局记成全歼。
    """
    assert parse_fleet_count("5.36K") == 5360
    assert parse_fleet_count(".36K") is None
