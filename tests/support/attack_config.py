"""攻击配置那几个旋钮的测试夹具。

⚠️ **住在 `support` 下，不是某个测试文件里。**

2026-08-26 踩过一次：`tests/e2e` 里写了
`from tests.integration.application.test_mission_scheduler import set_score_window`，
本机全绿、CI 直接收集失败（`ModuleNotFoundError: No module named 'tests'`）。

原因是两边的跑法不同 —— `pyproject.toml` 里配的是 `pythonpath = ["src", "tests"]`，
所以 `support.*` 到处都 import 得到，而 `tests.*` **本来就不是一个可导入的包**；
本机之所以绿，是因为我用 `python -m pytest`（它把当前目录塞进 `sys.path`），
而 CI 跑的是 `pytest`，不会。

⇒ 测试之间要共用东西，**放这里**，别跨测试包 import。
"""

from __future__ import annotations

from evo_helper.storage.repository import SqlAlchemyRepository


def set_score_window(
    repository: SqlAlchemyRepository,
    *,
    max_age_hours: float | None = None,
    window_floor: int | None = None,
) -> None:
    """把「选靶窗口」那两格写进**全局**攻击配置。

    2026-08-23 起有效期与窗口门限是全局的（`military_attack_config`），不再是任务
    参数——用户口径：「军力攻击的有效期 门限 改为全局设置，不再根据单个星系进行
    调整」。所以要摆一个窗口，摆的地方是这里，不是 `params_json`。

    ⚠️ **必须把现有的其它旋钮原样带回去。** `replace_military_attack_tiers` 是
    **整份替换**（那是它有意的语义：页面上就是整份 PUT），只送这两格等于把档位和
    其余十来个旋钮一起冲成空的——而症状会落在一条与本用例无关的判据上，
    排查起来是最贵的那一类。
    """
    try:
        row = repository.military_attack_config()
    except ValueError:
        # 配置行还没建（`prepare()` 才建它）。这条路上没有「其它旋钮」要保留，
        # 而 `replace_military_attack_tiers` 会顺手把 id=1 那一行建出来。
        repository.replace_military_attack_tiers(
            "[]", score_max_age_hours=max_age_hours, window_floor=window_floor
        )
        return
    repository.replace_military_attack_tiers(
        row.tiers_json,
        blind_scrolls=row.blind_scrolls,
        blind_scroll_rows=row.blind_scroll_rows,
        report_scan_hours=row.report_scan_hours,
        unknown_line_hold_minutes=row.unknown_line_hold_minutes,
        reconcile_cooldown_minutes=row.reconcile_cooldown_minutes,
        bot_revisit_hours=row.bot_revisit_hours,
        protection_exclusion_hours=row.protection_exclusion_hours,
        unreadable_exclusion_hours=row.unreadable_exclusion_hours,
        score_max_age_hours=max_age_hours,
        window_floor=window_floor,
        account_line_limit=row.account_line_limit,
        auto_toggle_log_seconds=row.auto_toggle_log_seconds,
    )
