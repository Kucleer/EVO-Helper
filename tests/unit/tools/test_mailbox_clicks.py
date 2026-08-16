"""翻信箱时**只许切到「报告」标签**，不许碰游戏里任何别的筛选控件。

用户口径（2026-08-11）：「你只能切换到报告，其他的筛选不要动。」

游戏信箱里的分类标签、排序、搜索这些是用户自己配好的。助手在那上面点一下，
下一轮翻到的就不是同一批邮件了，而这件事**不会报错**——只会表现成「战报读不到」，
和窗口太小、报告还没到长得一模一样，事后根本分不出是谁干的。

所以这条钉的不是某一次实现，而是**这条路径上允许出现的点击标签集合**：以后有人
（包括我）在翻信箱的流程里顺手加一次点击，这里就会红。
"""

from __future__ import annotations

import inspect

from evo_helper.tools import pirate_loop

#: 信箱这条路径上允许出现的点击标签。
#:
#: 「报告标签」是唯一被允许的筛选动作。其余全是开关面板与翻页：
#: 打开信箱、打开某一封、返回上一层、关掉面板/信箱。
ALLOWED_MAIL_CLICK_LABELS = frozenset(
    {
        "信箱",
        "报告标签",
        "打开邮件",
        "返回",
        "关闭面板",
        "关闭邮箱列表（左上角X）",
    }
)

#: 只审这几个方法——它们构成「进信箱、翻、读、出来」的完整路径。
MAILBOX_METHODS = (
    "_open_mail",
    "_close_mail",
    "_scan_mail_rows",
)


def _click_labels(source: str) -> set[str]:
    """抠出 `label="..."` 里的字面量。

    用文本扫描而不是跑一遍：跑起来要接管截屏、OCR 与整个导航，那样这条测试
    会因为无关的原因红，反而没人信它。
    """
    import re

    return set(re.findall(r'label="([^"]+)"', source))


def test_the_report_tab_is_the_only_filter_the_helper_touches() -> None:
    """本文件的重点。"""
    found: set[str] = set()
    for name in MAILBOX_METHODS:
        method = getattr(pirate_loop.PirateLoop, name, None)
        assert method is not None, f"{name} 不见了——这条测试守的路径变了，请更新它"
        found |= _click_labels(inspect.getsource(method))

    unexpected = found - ALLOWED_MAIL_CLICK_LABELS
    assert not unexpected, (
        f"信箱路径上出现了不该有的点击：{sorted(unexpected)}。"
        "用户口径：只能切到「报告」标签，其他筛选一律不动。"
    )


def test_the_report_tab_is_actually_used() -> None:
    """反过来也要成立：别哪天把切标签删了，却没人发现。

    没有它，信箱开在上次停留的分类上，翻到的邮件取决于用户上次点了哪个标签。
    """
    source = inspect.getsource(pirate_loop.PirateLoop._open_mail)
    assert "报告标签" in _click_labels(source)


def test_reconciliation_closes_the_mail_list_with_its_top_left_x() -> None:
    """对账收尾必须退出列表，否则下一步打开行星列表会被旧浮层遮住。"""

    class Driver:
        def __init__(self) -> None:
            self.clicks: list[tuple[int, int, str]] = []

        def click(self, x: int, y: int, *, label: str = "") -> None:
            self.clicks.append((x, y, label))

        def wait(self, _seconds: float) -> None:
            pass

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    driver = Driver()
    loop._driver = driver  # type: ignore[attr-defined]
    loop._on_mail_list = lambda: False  # type: ignore[attr-defined, assignment]
    loop._require_system_view = lambda _reason: None  # type: ignore[attr-defined, assignment]

    loop._close_mail()

    assert driver.clicks == [(750, 71, "关闭邮箱列表（左上角X）")]


def test_the_allow_list_has_no_filter_sounding_entries() -> None:
    """白名单本身也要守住——否则「加个标签就顺手加进白名单」也能让上面那条变绿。

    这条不认某个具体词，而是认「筛选类动作」这一类：白名单里除了「报告标签」
    以外，不许再出现带「标签 / 筛选 / 排序 / 搜索」字样的项。
    """
    filterish = {
        label
        for label in ALLOWED_MAIL_CLICK_LABELS
        if any(word in label for word in ("标签", "筛选", "排序", "搜索"))
    }
    assert filterish == {"报告标签"}
