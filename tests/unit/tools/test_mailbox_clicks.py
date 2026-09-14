"""翻信箱时能碰哪些标签，**清单就在这里**。

用户口径（2026-08-11）：「你只能切换到报告，其他的筛选不要动。」

⚠️ **2026-09-12 用户放开了一个例外**：「打开舰队的二级标签，你就可以看见回收报告」
——读回收实收必须点「报告」底下那一排的「舰队」。所以清单里多了二级标签那两下，
**而且只有那两下**：「侦察」「系统」以及一级的「个人/探索/收藏」仍旧不许碰。

⚠️⚠️ 例外是**带着还原义务**的：读完必须把舰队筛选**关回去**，否则下一趟战报那一趟会
在只剩舰队类的列表上开工，主题闸把它们全拒掉，日志上看着和「信箱里没有战报」
一模一样。下面 `test_the_fleet_tab_is_always_switched_back` 守的就是这一条。

⚠️ 2026-09-13 夜实机纠正：那一排里**只有「舰队」那个按钮有反应**，它是个
筛选开关而不是页签，所以「关回去」点的是**同一个坐标**，不是去点「战斗」。

游戏信箱里的分类标签、排序、搜索这些是用户自己配好的。助手在那上面点一下，
下一轮翻到的就不是同一批邮件了，而这件事**不会报错**——只会表现成「战报读不到」，
和窗口太小、报告还没到长得一模一样，事后根本分不出是谁干的。

所以这条钉的不是某一次实现，而是**这条路径上允许出现的点击标签集合**：以后有人
（包括我）在翻信箱的流程里顺手加一次点击，这里就会红。
"""

from __future__ import annotations

import inspect
import re

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
        # ⚠️ 2026-09-12 放开的那个例外，见模块头。**只有这两个**。
        "二级标签「舰队」",
        "二级标签「舰队筛选关」",
    }
)

#: 只审这几个方法——它们构成「进信箱、翻、读、出来」的完整路径。
MAILBOX_METHODS = (
    "_open_mail",
    "_close_mail",
    "_scan_mail_rows",
    # ⚠️ **切二级标签那一下写在这个方法里，不在上面三个里。** 少了这一行，
    # 这条用例会继续全绿却什么都不守：新加的点击藏在一个没被审的方法里
    # （`_scan_mail_rows` 只是调用它，源码里看不到那个 `label=`）。
    "_select_mail_sub_tab",
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
    # ⚠️ 2026-09-12 之前这里只有「报告标签」一个。放开成三个是**用户点名要的**
    # （「打开舰队的二级标签，你就可以看见回收报告」），不是顺手加的。
    # 再往里加任何一个之前，先回去读模块头那两段。
    assert filterish == {"报告标签", "二级标签「舰队」", "二级标签「舰队筛选关」"}


def test_the_fleet_tab_is_always_switched_back() -> None:
    """读完回收报告必须切回「战斗」标签。

    ⚠️ **这是那个例外的还原义务。** 停在舰队标签上的后果不是报错，是下一趟
    战报那一趟把整列「舰队返回」按主题拒掉——日志上和「信箱里没有战报」
    一模一样，而那正是 2026-08-13 那夜「17 发攻击 0 份战报」的同一类症状。
    """
    source = inspect.getsource(pirate_loop.PirateLoop.collect_recycle_hauls)
    assert "fleet=False" in source, (
        "读完回收报告没有切回「战斗」标签；停在舰队标签上会让下一趟战报那一趟静默读空。"
    )


def test_the_recycle_trip_is_off_by_default() -> None:
    """⚠️ 开关关着时**一步都不许走**，连信箱都不许进。

    用户口径（2026-09-12）：「我希望这是有个开关…我担心这花费我太多的时间」。
    默认开着等于替所有人决定每趟多花几分钟。

    ⚠️ 2026-09-13 这个旋钮从「每趟读几封」改成了纯开关（用户口径：
    「我只需要 on/off」），判据不变。
    """
    calls: list[int] = []
    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._ensure_run = lambda: calls.append(1)  # type: ignore[attr-defined, assignment]
    loop._scan_mail_rows = lambda **_kwargs: calls.append(2)  # type: ignore[attr-defined, assignment]

    assert loop.collect_recycle_hauls(enabled=False) is None
    assert calls == [], "开关关着时连库和信箱都不该碰"


def test_being_off_still_leaves_a_line_in_the_log() -> None:
    """⚠️⚠️ **关着也要说一句。**

    2026-09-13 实机代价：`recycle_mail_opens` 是 NULL，于是这条旁路静默 `return`，
    结果生产库里 233 发回收派遣全停在「待回收」、而 `system_log` 里
    **一个字都没有** —— 翻遍日志也查不出「为什么不读」，只能靠读源码。

    一条旁路可以不做事，**但不许不留痕**（同 `every-feature-needs-diagnosable-logs`）。
    """
    source = inspect.getsource(pirate_loop.PirateLoop._collect_recycle_hauls_if_enabled)
    off_branch = source.split("if not enabled:", 1)
    assert len(off_branch) == 2, "找不到「关着」那一支；这条用例过期了"
    # ⚠️ 按**行首缩进**切那条 `return`，不要按裸的 "return" 切：
    # 这一支的注释里就写着「静默 `return`」四个字，那么切会在注释处就断开，
    # 于是这条用例永远红 —— 而红的地方指着源码，错在用例（同 #333 那次）。
    body = off_branch[1].split("\n            return", 1)[0]
    assert "say(" in body, (
        "「关着」那一支直接 return 了，没留下任何日志 —— "
        "2026-09-13 就是这样让 233 发回收无声停在「待回收」的"
    )


def test_every_recycle_mail_leaves_evidence_behind() -> None:
    """⚠️ 用户口径 2026-09-13：「我需要开回收，并且记录对应邮件」。

    三条出路各有各的留痕，**一条都不许空手走**：
    读不出 → 现场图；认不上 → 现场图；入库 → 邮件那一屏进库，派遣日志上点得开。

    读不出那一档最要紧：信已经开了、内容看过了，什么都不留的话，
    下一趟它照样读不出，而没有原分辨率的现场，连「为什么」都无从查起
    （同 `get-the-pixels-before-changing-a-judgement`）。
    """
    source = inspect.getsource(pirate_loop.PirateLoop._ingest_recycle_haul)

    assert source.count("_dump_frame(") == 2, "读不出 / 认不上这两条出路要各留一张现场图"
    assert "_store_report_screenshot(" in source, "入库那一份没把邮件那一屏存下来"


# -- 二级标签：它是开关，不是页签（2026-09-13 夜实机） -----------------------------


def test_both_directions_click_the_same_button() -> None:
    """⚠️⚠️ 「舰队」是一个**筛选开关**，两个方向点的是同一个坐标。

    2026-09-13 夜实机逐点验过那一排四个按钮，只有「舰队」有反应；
    原先还有一个 `MAIL_BATTLE_SUB_TAB`，它①用的是「侦察」的 x，②就算改对，
    那个按钮也不响应——后果是「读完回收报告切回去」实测 3/3 全失败。

    这一条钉的是「别再加回来一个方向专用的坐标」。
    """
    # ⚠️ 判**行首的赋值**，不判「这三个字出现过」：那个常量为什么被删掉，正写在
    # `MAIL_FLEET_SUB_TAB` 的注释里，而注释里必然带着它的名字。按出现判的话，
    # 这条用例会把那段说明本身判成违规 —— 红的地方指着注释，错在用例。
    constants = inspect.getsource(pirate_loop).split("class PirateLoop", 1)[0]

    assert not re.search(r"^MAIL_BATTLE_SUB_TAB\s*=", constants, re.M), (
        "又出现了一个方向专用的二级标签坐标；那一排只有「舰队」那个按钮有反应"
    )

    # 反过来：切回去那一下必须真的走同一个坐标 —— `target` 只许被赋一次，
    # 而且赋的就是那个开关。
    # ⚠️ 同上：数「出现几次」会把注释里提到的那次也数进去（我刚踩过）。
    source = inspect.getsource(pirate_loop.PirateLoop._select_mail_sub_tab)
    targets = re.findall(r"^\s*target = (.+)$", source, re.M)
    assert targets == ["MAIL_FLEET_SUB_TAB"], (
        f"两个方向应当共用同一个 target，实际是 {targets}；分叉就说明又按方向挑坐标了"
    )


def test_it_looks_before_it_clicks() -> None:
    """⚠️⚠️ **先看再点。** 它是开关，而且状态跨次记住。

    已经在目标档位上时再点一下会把它**翻掉**。原先是无条件先点再看，于是
    「上一趟停在舰队」这一种情形下第一下就翻走、判据对不上、重试再点又翻回来——
    本该一下到位的一个按钮变成了赌奇偶，而那正是那一夜「切不到舰队，整趟不读」的成因。
    """
    source = inspect.getsource(pirate_loop.PirateLoop._select_mail_sub_tab)
    before_click = source.split("self._driver.click(", 1)[0]

    assert "_sub_tab_matches(" in before_click, (
        "点之前没有先看一眼；它是开关，已经到位时再点一下会翻掉"
    )


def test_the_click_label_dodges_the_read_only_gate() -> None:
    """⚠️⚠️ 点击标签里**一个 `FORBIDDEN_LABELS` 都不许沾**。

    那个字符串不是给人看的说明文字——`human_input._reject_acting_label` 拿它当安全闸，
    而回收那一趟正是 `allow_actions=False` 跑的。2026-09-13 夜这里连撞两次：
    「默认（含攻击报告）」中了「攻击」，「取消舰队筛选」又中了「取消」。
    真合进去就是回收趟当场崩。
    """
    from evo_helper.game.human_input import FORBIDDEN_LABELS

    source = inspect.getsource(pirate_loop.PirateLoop._select_mail_sub_tab)
    names = re.findall(r'name = "([^"]*)" if fleet else "([^"]*)"', source)
    assert names, "找不到那两个标签名；这条用例过期了"
    for label in names[0]:
        for word in FORBIDDEN_LABELS:
            assert word not in label, f"标签「{label}」里有 {word!r}，只读进程点它会被拒"


# -- 2026-09-14 那次「攻击战报断流 9 小时」教出来的三条 -------------------------


def test_both_directions_need_positive_evidence() -> None:
    """⚠️⚠️ **两个方向都要正面证据，一次都不许只靠「没看见对方」。**

    2026-09-13 夜我把「关掉筛选」那一侧放松成了「没有舰队类就算切回去了」，
    理由是攻击报告主题读不准、正面证据拿不到。代价 9 小时后显形：
    信箱一直停在舰队筛选上，而**读战报那一趟就在只剩舰队类的列表上开工** ——
    整屏按主题全拒、日志上一切正常，09-13 21h 起攻击战报断流、37 发无战报。

    破绽当时就写在日志里：

        二级标签在「舰队筛选关」上：这一屏 6 行里舰队类 0 行、攻击报告 0 行，对得上

    「两个都是 0」既可能是没筛过的列表，也可能是半加载、甚至根本不在列表上。
    """
    source = inspect.getsource(pirate_loop.PirateLoop._sub_tab_matches)
    # ⚠️ 只看 `good = ` 那一行，**不要扫整段源码**：这次事故的成因就写在上面的
    # 注释里，而注释里必然带着 `here == 0` 这几个字。按整段扫的话，这条用例会把
    # 那段说明本身判成违规 —— 红的地方指着注释，错在用例。
    # （同一个坑今天已经踩到第三次了：#333 的 colspan、#336 的 MAIL_BATTLE_SUB_TAB。）
    verdicts = re.findall(r"^\s*good = (.+)$", source, re.M)
    assert len(verdicts) == 1, f"判据不止一行，或者找不到：{verdicts}"
    verdict = verdicts[0]

    assert "other > 0" in verdict, (
        f"「关掉筛选」那一侧又变回只靠「没看见舰队类」了（判据：{verdict}）—— "
        "那正是 2026-09-14 那次攻击战报断流 9 小时的成因"
    )
    assert "here == 0" not in verdict, f"判据里出现了「没有舰队类就算数」这种反向断言：{verdict}"


def test_the_report_trip_guarantees_its_own_precondition() -> None:
    """⚠️⚠️ **读战报那一趟自己保证「舰队筛选是关的」，不指望回收那一趟收拾干净。**

    那条「读完要切回去」的还原义务原先只写在回收那一侧，于是它一旦失手，
    代价全落在战报这一侧 —— 而这一侧毫不知情，照常开工、什么都没找到，
    日志上和「信箱里真的没有战报」一模一样。**9 小时没人发现。**
    """
    source = inspect.getsource(pirate_loop.PirateLoop._scan_for_reconcile)

    assert "_select_mail_sub_tab(fleet=False)" in source, (
        "读战报那一趟没有自己关掉舰队筛选；它不能指望别人收拾干净"
    )
    assert "raise RuntimeError" in source, (
        "关不掉筛选时没有放弃这一趟 —— 在筛过的列表上翻等于静默读空，而那和真的没有战报分不出来"
    )


def test_only_the_recycle_trip_may_be_strict_about_subjects() -> None:
    """⚠️ 用户口径 2026-09-14（连说三遍）：「舰队返回不要开信」。

    回收那一趟因此连主题读不出的行也不开。**但战报那一趟绝不许开这个档** ——
    那一侧「漏开一封 = 再也读不回来」，方向正好相反。
    """
    recycle = inspect.getsource(pirate_loop.PirateLoop.collect_recycle_hauls)
    assert "strict_subject=True" in recycle, "回收那一趟没开严格主题档；舰队返回还会被开"

    for name in ("_scan_for_reconcile", "_scan_for_scout_reports"):
        method = getattr(pirate_loop.PirateLoop, name, None)
        if method is None:
            continue
        assert "strict_subject" not in inspect.getsource(method), (
            f"{name} 开了严格主题档 —— 那一侧漏开一封就是永久丢一份报告"
        )
