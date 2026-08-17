"""控制台的显示表：只管好看，不管判据。"""

from __future__ import annotations

from evo_helper.application.backfill import BackfillPhase
from evo_helper.domain.records import TARGET_KIND_LABELS
from evo_helper.domain.scheduler import MissionKind, TaskStatus
from evo_helper.game.pirate_ui import PIRATE_TRIGGER_SHIPS
from evo_helper.web.display import (
    BACKFILL_PHASE_GLYPHS,
    BACKFILL_PHASE_TONES,
    BATTLE_RESULT_LABELS,
    DISPATCH_STATE_LABELS,
    LIST_SHIP_COLUMNS,
    MISSION_LABELS,
    STATUS_GLYPHS,
    STATUS_TONES,
    TARGET_KIND_GLYPHS,
    TARGET_KIND_TONES,
    missing_backfill_kind_labels,
    missing_backfill_phases,
    missing_intel_labels,
    missing_status_tones,
    settled_score,
)


def test_every_status_has_its_own_slot() -> None:
    """八档一个都不能少。

    页面按状态上色，色调表里少一格就意味着有两档被当成了同一件事——而恰恰是
    「未启用 / 待命」与「冷却中 / 等航线」这两对最不能混：没勾的任务显示
    「待命」是谎话，冷却中显示「等航线」会让用户去调航线数、调完还是不动。
    """
    assert missing_status_tones() == []
    assert len(STATUS_TONES) == len(TaskStatus)
    assert len(STATUS_GLYPHS) == len(TaskStatus)


def test_no_two_statuses_share_a_glyph() -> None:
    """色永远配一个字形（`console.css` 顶部那条）。

    两档共用一个字形，在灰度下、对色盲用户就等于合并了——这一层存在的
    全部理由就是不让它们合并。
    """
    assert len(set(STATUS_GLYPHS.values())) == len(TaskStatus)


def test_every_mission_kind_has_a_label() -> None:
    """标签由服务端下发，页面和桌面悬浮窗都不自己拼。"""
    assert set(MISSION_LABELS) == {kind.value for kind in MissionKind}


def test_the_list_has_no_per_ship_columns() -> None:
    """情报中心列表只看舰队总数，不再逐舰种开列（用户口径 2026-08-11）。

    移除的理由是**数据源**，不是版面：bot 那半边根本没有这四个数（逐舰种明细在
    战斗回放页上，而 bot 链路刻意只读详情页），海盗那半边的 `收割者` 一列在实机
    98 份报告里一份都没读出来。摆着的是满屏的「—」，而补齐它要多标定一个按钮、
    每份报告多花两三秒 OCR——用户选择不付这笔钱。

    钉成空元组而不是删掉常量：取数与渲染那条路仍然按它走，回放页哪天标定好了，
    把 `PIRATE_TRIGGER_SHIPS` 填回去就有列。
    """
    assert LIST_SHIP_COLUMNS == ()
    # 判定舰种本身没被删——它仍然是侦察判定的依据，只是不再上列表。
    assert len(PIRATE_TRIGGER_SHIPS) == 4


def test_every_intel_state_has_its_own_slot() -> None:
    """派遣结果、战果、目标类型三张表一档都不能缺。

    缺一档，页面上就会冒出一个没人翻译过的英文常量；更糟的是两档被当成同一件事，
    而「未派出（被闸门拦下）」与「被拒（游戏没接受）」正是最不能混的一对——
    它们对应两种完全不同的排查方向。
    """
    assert missing_intel_labels() == []


def test_bot_and_pirate_do_not_share_a_colour_or_a_glyph() -> None:
    """列表里 bot 与海盗要一眼分得开，而色不是唯一的信号。

    共用同一个 tone 就等于没分；共用同一个字形，在灰度下、对色盲用户同样等于没分。
    """
    assert len(set(TARGET_KIND_TONES.values())) == len(TARGET_KIND_LABELS)
    assert len(set(TARGET_KIND_GLYPHS.values())) == len(TARGET_KIND_LABELS)


def test_no_two_dispatch_states_share_a_label() -> None:
    """四档各是一句不同的话。两档写成同一个词，筛选结果就解释不通。"""
    assert len(set(DISPATCH_STATE_LABELS.values())) == len(DISPATCH_STATE_LABELS)


def test_no_two_battle_results_share_a_label() -> None:
    assert len(set(BATTLE_RESULT_LABELS.values())) == len(BATTLE_RESULT_LABELS)


def test_every_backfill_phase_has_its_own_slot() -> None:
    """补录六档一个都不能少。

    最不能混的是「补录完成」与「补录失败」：失败意味着那批战报还躺在信箱里，
    而任务马上要拿这份仍然不全的数据去决定要不要再打一遍。
    """
    assert missing_backfill_phases() == []
    assert len(BACKFILL_PHASE_TONES) == len(BackfillPhase)


def test_no_two_backfill_phases_share_a_glyph() -> None:
    """色永远配一个字形：两档共用一个字形，在灰度下、对色盲用户就等于合并了。"""
    assert len(set(BACKFILL_PHASE_GLYPHS.values())) == len(BackfillPhase)


def test_every_backfill_chain_has_a_label() -> None:
    """页面上那个下拉框按它建，漏一条就等于那条链路在页面上补不了。"""
    assert missing_backfill_kind_labels() == []


def test_a_historic_float_tail_is_settled_before_it_reaches_the_page() -> None:
    """⚠️ **恰好相等，不许用 `pytest.approx`。**

    这三个是 2026-08-17 军力榜页面上原样出现的值。源头已经在
    `tools.ranking_scan.parse_score` 修掉（改走 `Decimal`），但库里存着的
    那一批只能在读出来的这一步收——用户口径：开发过程不碰生产库，历史值不许
    UPDATE，重采时自然覆盖。
    """
    assert settled_score(64959.99999999999) == 64960
    assert settled_score(64260.00000000001) == 64260
    assert settled_score(64180.00000000001) == 64180


def test_the_m_scale_tail_is_settled_too() -> None:
    """M 量级的误差绝对值大得多（1e-7 而不是 1e-11），两位小数照样收得住。

    钉住它是因为「按固定小数位取整」很容易只在 K 量级上验过就收工。
    """
    assert settled_score(404169999.99999994) == 404170000


def test_an_interpolated_half_survives_the_settling() -> None:
    """⚠️ **`.5` 不许被一起抹掉。**

    `domain.ranking.interpolate_scores` 取的中点在两个已知值之和为奇数时必然
    带 `.5`（页面上的 `72252.5 (估算)` 就是），那是合法值不是误差。
    收敛到整数位能让脏值更好看，代价是把这个真值报错——所以刻度停在两位小数。
    """
    assert settled_score(72252.5) == 72252.5
    assert settled_score(64252.5) == 64252.5


def test_an_unknown_score_stays_unknown() -> None:
    """**猜出来的数不许长得像量出来的**，`None` 更不许变成 `0`。"""
    assert settled_score(None) is None
