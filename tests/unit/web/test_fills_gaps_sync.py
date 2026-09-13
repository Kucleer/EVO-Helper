"""页面那份「填空隙任务」清单必须和领域层的 `GAP_FILLERS` 一字不差。

⚠️ 这两份清单**没有任何机制让它们同步**：一份是 Python 的 frozenset，
一份是 `missions.html` 里的 JS 字面量数组。

漏改的后果是静默的：那张卡在页面上变成可拖的，而带 `priority` 的 PATCH
打到填空隙任务身上一律 400 —— 用户拖完了才发现。

2026-09-13 加星门时就漏了一次。
"""

from __future__ import annotations

import re
from pathlib import Path

from evo_helper.domain.scheduler import GAP_FILLERS

TEMPLATE = Path(__file__).resolve().parents[3] / "src/evo_helper/web/templates/missions.html"


def test_the_page_list_matches_the_domain_set() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    match = re.search(r"const FILLS_GAPS = \[([^\]]*)\];", text)

    assert match is not None, "missions.html 里找不到 FILLS_GAPS —— 这条用例守的东西改名了"
    listed = {item.strip().strip("'\"") for item in match.group(1).split(",") if item.strip()}

    assert listed == {kind.value for kind in GAP_FILLERS}, (
        "页面的 FILLS_GAPS 和 domain.scheduler.GAP_FILLERS 对不上。"
        "多了会让本可拖的卡拖不动；少了会让那张卡可拖，而拖一下就吃一个 400。"
    )
