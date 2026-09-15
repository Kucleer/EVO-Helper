"""翻信箱时能碰哪些标签，**清单就在这里**。

用户口径（2026-08-11）：「你只能切换到报告，其他的筛选不要动。」

⚠️ **2026-09-12 用户放开了一个例外**：「打开舰队的二级标签，你就可以看见回收报告」
——读回收实收必须点「报告」底下那一排的「舰队」。所以清单里多了二级标签那两下，
**而且只有那两下**：「侦察」「系统」以及一级的「个人/探索/收藏」仍旧不许碰。

⚠️⚠️ 例外是**带着还原义务**的：读完必须把舰队筛选**关回去**，否则下一趟战报那一趟会
在只剩舰队类的列表上开工，主题闸把它们全拒掉，日志上看着和「信箱里没有战报」
一模一样。下面 `test_the_fleet_tab_is_always_switched_back` 守的就是这一条。

⚠️⚠️ 2026-09-15 实拍再次纠正：`#336` 说「那一排只有『舰队』有反应、它是开关」——
**那个结论是错的**。取消舰队之后不是「显示全部」，是**什么都不显示**
（第二排一个筛选都没选中时，列表正中写着「没有符合当前筛选条件的邮件。」，
而角标写着战斗 11 封、舰队 99+ 封）。所以「切回去」= **点「战斗」**，
整段经过在 `MAIL_BATTLE_SUB_TAB` 上。代价：攻击战报整晚读不到。

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
        "二级标签「战斗」",
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
    assert filterish == {"报告标签", "二级标签「舰队」", "二级标签「战斗」"}


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


def test_the_two_directions_click_different_buttons() -> None:
    """⚠️⚠️⚠️ **要舰队就点舰队，要战报就点「战斗」——两个不同的按钮。**

    `#336` 曾经把这一排判成「只有『舰队』有反应、它是个开关」，于是「切回去」
    实现成**再点一次舰队把它取消**。2026-09-15 实拍推翻了那个模型
    （`dump-mail-list-empty-014934.png`）：

        战斗 [11]   侦察   舰队 [99+]   系统
        列表正中：「没有符合当前筛选条件的邮件。」

    **取消舰队之后不是「显示全部」，是什么都不显示。** 角标明明写着战斗 11 封、
    舰队 99+ 封，而第二排一个筛选都没选中时列表是空的。

    代价：**攻击战报整晚读不到**（09-14 06:37 → 09-15 02:00，一份都没有）。

    ⚠️ 而 `#336` 之前的代码本来就是点「战斗」切回去的，只是坐标抄错
    （897 是**侦察**的 x）。那个方向是对的，被我当成错的删掉了。
    """
    constants = inspect.getsource(pirate_loop).split("class PirateLoop", 1)[0]

    for name in ("MAIL_FLEET_SUB_TAB", "MAIL_BATTLE_SUB_TAB"):
        assert re.search(rf"^{name}\s*=\s*\(", constants, re.M), f"{name} 不见了"

    assert pirate_loop.MAIL_FLEET_SUB_TAB != pirate_loop.MAIL_BATTLE_SUB_TAB, (
        "两个方向又共用一个坐标了 —— 那是 `#336` 的错误模型，"
        "取消舰队之后列表会变空，攻击战报就整晚读不到"
    )
    # ⚠️ 实拍量出来的 x：战斗跨 713~830、舰队跨 965~1082（`dump-mail-list-empty-014934.png`）。
    assert 713 <= pirate_loop.MAIL_BATTLE_SUB_TAB[0] <= 830, "战斗的 x 落到别的按钮上了"
    assert 965 <= pirate_loop.MAIL_FLEET_SUB_TAB[0] <= 1082, "舰队的 x 落到别的按钮上了"

    # 两个方向必须**按 fleet 挑**坐标，不许写死一个。
    source = inspect.getsource(pirate_loop.PirateLoop._select_mail_sub_tab)
    targets = re.findall(r"^\s*target = (.+)$", source, re.M)
    assert targets == ["MAIL_FLEET_SUB_TAB if fleet else MAIL_BATTLE_SUB_TAB"], (
        f"切标签没有按方向挑坐标：{targets}"
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


def test_the_filter_is_only_judged_in_the_direction_that_reads() -> None:
    """⚠️⚠️⚠️ **只在「筛选开着」这个方向上判，那是唯一拿得到正面证据的方向。**

    我在这个不对称上栽了两次，方向相反，两次都上了生产：

    - 2026-09-13 夜：把「关掉」那一侧放松成「没看见舰队类就算数」——
      筛选卡在舰队上没人发现，**攻击战报断流 9 小时、37 发无战报**。
    - 2026-09-14 午：把它改严成「必须正面看见非舰队类」——
      而非舰队类主题**本来就读不出**，于是永远确认不了、每轮在开工那步自杀，
      **全线停摆 57 分钟，一发都派不出去**。

    实测事实只有一条：**舰队类主题读得准（6/6），非舰队类读不准。**
    所以「开着」要正面证据，「关掉了」= 认不出开着，不去正面确认。
    """
    source = inspect.getsource(pirate_loop.PirateLoop._sub_tab_matches)
    # ⚠️ 只看判据那两行，别扫整段 —— 上面那段注释里必然带着两次事故的反面写法。
    on_lines = re.findall(r"^\s*on = (.+)$", source, re.M)
    verdicts = re.findall(r"^\s*good = (.+)$", source, re.M)
    assert len(on_lines) == 1 and len(verdicts) == 1, f"判据不止一处：{on_lines} / {verdicts}"

    assert on_lines[0] == "here > 0 and other == 0", (
        f"「筛选开着」的正面判据被改了：{on_lines[0]}。"
        "⚠️ 这个阈值是照两组日志计数**挑**出来的（不是「216 次生产实测」，那是作废口径）："
        "问开着的 28 行落在 4~6、问关掉的 16 行落在 0；"
        "问「关掉了？」的 16 行落在 0（⚠️ 正常运行期；原先写的 188 里有 172 条"
        "来自故障窗口，已作废）。收紧成「整屏全中」只会认出更少 —— 那 28 行里"
        "有 54% 不是满屏。⚠️ 这是日志计数比例，不是漏检率：这批样本没有独立状态标签。"
    )
    assert verdicts[0] == "on if fleet else not on", (
        f"两个方向不再共用同一个正面探测了：{verdicts[0]} —— "
        "「关掉了」一旦去正面确认，就会因为非舰队类读不出而永远判不成立"
    )


def test_the_unfiltered_trips_guarantee_their_own_precondition() -> None:
    """⚠️⚠️ **要没筛过列表的那几趟，自己保证筛选是关的。**

    「读完切回去」的还原义务原先只写在回收那一侧；它一失手，代价全落在读战报
    那一侧，而那一侧毫不知情 —— 2026-09-13 夜就是这样烧了 9 小时、37 发无战报。

    这一条同时钉住**位置**和**处置**，两样都是 2026-09-14 当天拿生产换来的：

    - 位置必须在 `_enter_mailbox()` **之后** —— 第一版写在 `_scan_for_reconcile`
      开头，那时信箱还没开，探测读的是恒星系视图、一行都读不到，
      **每轮白花一次 OCR 而一点保护都没有**。
    - 处置只许走 `cut_short`，**不许抛** —— 同一天写过「关不掉就抛异常」，
      而「关不掉」在主题读不出时必然成立，于是每轮在开工那步自杀，
      **攻击、扫描全停 57 分钟、216 轮全判失败**。
    """
    source = inspect.getsource(pirate_loop.PirateLoop._scan_mail_rows)

    assert "not fleet_sub_tab and self._fleet_filter_looks_on()" in source, (
        "收手的条件不再以「正面认出筛选开着」打头 —— 只要主题读不出，它就会每趟都收手"
    )
    assert source.index("self._enter_mailbox()") < source.index("_fleet_filter_looks_on"), (
        "那道探测排在开信箱之前 —— 它读不到列表，等于没装"
    )
    tail = source[source.index("_fleet_filter_looks_on") :][:700]
    assert "cut_short=" in tail, "关不掉筛选时没有走 cut_short"
    assert "raise" not in tail, "关不掉筛选时又去抛异常了 —— 那正是停摆 57 分钟的成因"


def test_only_the_recycle_trip_skips_read_unknowns() -> None:
    """⚠️ 用户口径 2026-09-14：「舰队返回不要开信」+「读未读配色的，不是已读配色」。

    ⚠️⚠️ **第一版（`#339` 的 `strict_subject`）是「主题读不出就一律不开」，错的。**
    实测列表行主题 OCR 有 87% 读不出（昨夜 601 封开封里 522 封是这一档），
    回收报告正在其中 —— 上线后回收读信当场变成每趟 0 份，而库里还有 317 发在等实收。

    现在改用未读色当闸：**只有正面认出「已读」才跳过**。

    **但战报那一趟绝不许开这个档** —— 那一侧「漏开一封 = 再也读不回来」，方向相反。
    """
    recycle = inspect.getsource(pirate_loop.PirateLoop.collect_recycle_hauls)
    assert "skip_read_unknowns=True" in recycle, "回收那一趟没开这道闸"
    assert "strict_subject" not in recycle, (
        "又出现了「主题读不出就一律不开」那一档 —— 它会把 87% 的回收报告一起挡死"
    )

    for name in ("_scan_for_reconcile", "_scan_for_scout_reports"):
        method = getattr(pirate_loop.PirateLoop, name, None)
        if method is None:
            continue
        src = inspect.getsource(method)
        assert "skip_read_unknowns" not in src and "strict_subject" not in src, (
            f"{name} 开了这道闸 —— 那一侧漏开一封就是永久丢一份报告"
        )


def test_unreadable_subjects_are_opened_unless_positively_read() -> None:
    """⚠️⚠️ **判据方向：只有 `unread is False` 才跳过。**

    `MailRow.unread` 的语义是 `None` = 「颜色读不出」，而它的注释写明
    **「读不出绝不能往已读那一侧倒」**（判成已读的代价高得多）。

    这条钉住三档的处置，因为搞反任何一档的症状都是「回收报告读不出来」，
    而那在日志上和「信箱里没有回收报告」长得一样 —— 2026-09-14 已经吃过一次。
    """
    source = inspect.getsource(pirate_loop.PirateLoop._scan_mail_rows)
    gates = re.findall(r"^\s*if skip_read_unknowns and (.+):$", source, re.M)
    assert len(gates) == 1, f"这道闸不止一处，或者找不到：{gates}"

    assert gates[0] == "row.kind is ReportKind.UNKNOWN and row.unread is False", (
        f"闸门判据被改了：{gates[0]}。"
        "⚠️ 必须是 `row.unread is False`（正面认出已读才跳过）——"
        "写成 `not row.unread` 会把 `None`（颜色读不出）一起跳掉，"
        "那就退回成第一版那个把 87% 回收报告挡死的行为。"
    )


# -- 判据的**行为**，不是它的源码长什么样 ---------------------------------------


def _judge(kinds: list[object], *, fleet: bool) -> bool:
    """拿一屏假的行类型去问判据。"""

    class Row:
        def __init__(self, index: int, kind: object) -> None:
            # ⚠️ `subject` 不能省：`mail_list_is_empty` 要读它（空列表那道闸）。
            # 真实的 `MailRow` 一定有这一格。
            self.index, self.kind, self.subject = index, kind, "某封信"

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._mail_list_rows = lambda **_kwargs: [  # type: ignore[assignment]
        Row(i, k) for i, k in enumerate(kinds)
    ]
    return loop._sub_tab_matches(fleet=fleet, name="x", quiet=True)


def test_the_judgement_behaves_right_on_the_screens_that_actually_happened() -> None:
    """⚠️⚠️ **这一条验的是行为，上面那两条只验源码文本。**

    源码守卫写对了判据行就会绿，而 2026-09-14 那次**判据行本身写得漂亮、
    在生产上却每轮自杀** —— 源码守卫一个字都拦不住（同 `tests-can-guard-a-bug`）。

    ⚠️ **口径说明**（2026-09-14 复核 Codex 指出，原措辞说得过满）：
    这里输入的是**构造出来的行类型数组**，不是「截图 → OCR → 判据」的回放。
    它验的是**判据函数本身**在各种计数组合下的取值；真实屏幕到类型数组那一段
    （也就是 OCR 那一段）**不在这条用例的覆盖范围内**，那要靠实机。

    ⚠️ **下面的组合不都是现场计数**（2026-09-14 复核第二轮指出）：
    「全 unknown」「6/6 全中」对应真实观察到的计数；而 3/6 那一组是**构造出来的更严一档**
    （现场观察到的最低是 4/6），用来确认阈值不会把低读数的开着屏漏掉。
    """
    F = pirate_loop.ReportKind.FLEET_RETURN
    R = pirate_loop.ReportKind.RECYCLE
    A = pirate_loop.ReportKind.ATTACK
    U = pirate_loop.ReportKind.UNKNOWN

    # 2026-09-14 生产现场：读不出主题的一整屏。
    # ⚠️ 这一屏必须放行 —— 它正是让每一轮自杀、全线停摆 57 分钟的那一屏。
    assert _judge([U] * 6, fleet=False) is True, "读不出主题的一屏必须当作「筛选没开着」放行"
    assert _judge([U] * 6, fleet=True) is False, "读不出主题时不许说「筛选开着」"

    # 2026-09-13 夜实测：筛选真开着时 6/6 全中。
    assert _judge([F, F, R, F, R, F], fleet=True) is True, "筛选真开着必须认得出来"
    assert _judge([F, F, R, F, R, F], fleet=False) is False, (
        "筛选真开着时不许说「关掉了」—— 那正是 9 小时静默饿死的成因"
    )

    # 没筛过的列表：混着攻击报告。
    assert _judge([F, U, A, U, R, U], fleet=False) is True

    # ⚠️ 筛选开着、但这一屏**只读出 3/6**（实测最低见过 4/6，这里取更严的一档）
    # ——必须照样认出来。复核时我差点把判据收紧成「整屏全中」，
    # 而「整屏全中」严格强于 `here > 0`，只会认出更少：
    # 问「开着？」的那 28 行里只有 46% 是 6/6。
    # ⚠️ 这是**日志计数比例**，不是「漏检率」—— 这批样本没有独立状态标签
    # （2026-09-14 第四轮复核指出）。
    #
    # ⚠️ 这一行原先注释成「4/6」，是数错了（2026-09-14 复核 Codex 指出）：
    # [F, U, F, U, R, U] 里舰队类是 F、F、R **三个**。
    assert _judge([F, U, F, U, R, U], fleet=True) is True, (
        "只读出 3/6 的开着屏被漏判了 —— 实测 5/6 占 42%、4/6 占 10%"
    )
    assert _judge([F, U, F, U, R, U], fleet=False) is False

    # 混进一封确凿的非舰队类 ⇒ 不是筛过的列表。
    assert _judge([F, U, A, U, R, U], fleet=True) is False

    # 空屏（列表还没画出来）：两个方向都不许说「开着」。
    assert _judge([], fleet=True) is False, "一行都没读到时不许说「筛选开着」"


def test_doubt_alone_never_kills_the_round() -> None:
    """⚠️⚠️ **「查不出来」不许判死这一轮** —— 2026-09-14 停摆 57 分钟的那一条。

    这里直接跑那道前提检查：给它生产当时那一屏（全读不出），它必须**什么都不做**，
    既不点标签也不抛异常。
    """
    clicks: list[str] = []

    class Driver:
        def click(self, _x: int, _y: int, *, label: str = "") -> None:
            clicks.append(label)

        def wait(self, _seconds: float) -> None:
            pass

    class Row:
        index, kind, subject = 0, pirate_loop.ReportKind.UNKNOWN, "读不出的一行"

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._driver = Driver()  # type: ignore[attr-defined]
    loop._on_mail_list = lambda: True  # type: ignore[attr-defined, assignment]
    loop._mail_list_rows = lambda **_kwargs: [Row() for _ in range(6)]  # type: ignore[assignment]

    assert loop._fleet_filter_looks_on() is False, (
        "读不出主题的一屏被判成「筛选开着」—— 那会让前提检查去点标签、然后放弃整趟"
    )
    assert clicks == [], "只是探测一下，不该点任何标签"


def _scan_that_gives_up(*, fleet_sub_tab: bool, filter_looks_on: bool):
    """真跑一趟 `_scan_mail_rows`，让它在切标签那一步放弃，把返回对象交出来。

    ⚠️ **必须真跑，不能扫源码。** 见下面那条用例的说明。
    """
    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._enter_mailbox = lambda: None  # type: ignore[attr-defined, assignment]
    loop._select_mail_sub_tab = lambda **_kwargs: False  # type: ignore[assignment]
    loop._fleet_filter_looks_on = lambda: filter_looks_on  # type: ignore[assignment]
    loop._dump_frame = lambda *_a, **_k: None  # type: ignore[attr-defined, assignment]
    return loop._scan_mail_rows(
        wanted=pirate_loop.ReportKind.ATTACK,
        label="攻击战报",
        visit=lambda _row, _page: True,
        fleet_sub_tab=fleet_sub_tab,
    )


def test_a_trip_that_scanned_nothing_is_never_recorded_as_swept() -> None:
    """⚠️⚠️ **切不了二级标签的那一趟必须 `aborted=True`，否则对账时刻照写。**

    2026-09-14 复核（Codex）第一轮指出的 P1：两处提前返回都只给了 `cut_short`，
    而 `tally.swept = not deadline_hit and not aborted` **不看 `cut_short`** ——
    于是一趟什么都没扫的行程被记成「翻完了」，`record_daily_reconciliation` 照写，
    后面的轮次因冷却而跳过。这和 09-13 夜那次事故是**同一个病**：
    失败的一趟被记成成功的一趟。

    ⚠️⚠️⚠️ **这条用例的第一版是扫源码文本的，而它守不住任何东西**
    （复核第二轮指出）：两处 `return` 里的**注释**也含 `aborted=True` 这串字，
    所以把真参数删掉、只留注释，断言照样成立 —— 我当场试过，确实照样绿。

    **源码文本匹配分不清代码和注释**（同 `guards-must-read-the-verdict-not-the-whole-source`，
    两天内第四次栽在这一类上）。所以现在**真跑那两条路径、断言返回对象的值**。
    """
    # ① 回收方向：切不到「舰队」。这一处是 #336 就有的，同样漏过 aborted。
    recycle = _scan_that_gives_up(fleet_sub_tab=True, filter_looks_on=False)
    assert recycle.aborted is True, "切不到「舰队」标签却没标 aborted —— 对账时刻会被推进"
    assert recycle.cut_short, "放弃了却没说理由"

    # ② 战报方向：正面认出筛选开着，但关不掉。
    report = _scan_that_gives_up(fleet_sub_tab=False, filter_looks_on=True)
    assert report.aborted is True, "筛选关不掉却没标 aborted —— 对账时刻会被推进"
    assert report.cut_short, "放弃了却没说理由"

    # ③ ⚠️ 真正要守的是**下游那个结论**：`swept` 必须为 False。
    #    上面两个断言只管字段，这一条管它被怎么用 —— 判据抄自 `_scan_for_reconcile`。
    for scan in (recycle, report):
        swept = not scan.deadline_hit and not scan.aborted
        assert swept is False, (
            "这一趟一封都没看过，却会被判成「翻完了」并写进 daily_reconciliations"
        )


def test_giving_up_does_not_look_like_a_normal_empty_trip() -> None:
    """⚠️ 放弃的那一趟和「信箱里真的没有战报」**必须能分得出来**。

    分不出来正是 09-13 夜烧掉 9 小时的原因。`cut_short` 那句人话是给人看的，
    `aborted` 那一格是给代码看的 —— 两样都要有。
    """
    given_up = _scan_that_gives_up(fleet_sub_tab=False, filter_looks_on=True)
    normal = pirate_loop.MailScan(unread_budget=0)

    assert (given_up.aborted, bool(given_up.cut_short)) == (True, True)
    assert (normal.aborted, bool(normal.cut_short)) == (False, False)


def _opens_with(unread: bool | None, *, skip_read_unknowns: bool) -> bool:
    """主题读不出、未读色是 `unread` 的那一行，会不会被打开。**真跑 `_scan_mail_rows`。**"""
    opened: list[int] = []

    from datetime import UTC, datetime

    row = pirate_loop.MailRow(
        index=0,
        subject="~~ — —w————",  # 真实现场里的一行：主题读不出
        raw_time_text="14/09/2026 12:00:00",
        # ⚠️ 时刻必须读得出：这才是**真邮件行**的样子。
        # 整屏没时刻会被 `mail_list_looks_unrendered` 判成「不是邮件列表」，
        # 那条闸守的是另一件事（登录页上的幻影行）。
        reported_at_utc=datetime(2026, 9, 14, 12, 0, tzinfo=UTC),
        kind=pirate_loop.ReportKind.UNKNOWN,
        unread=unread,
    )

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    # ⚠️ `_scan_mail_rows` 起手会核视口（`_ensure_geometry`），那一步要真窗口。
    # 这条用例验的是开封判据，不是几何，所以桩掉。
    loop._ensure_geometry = lambda: None  # type: ignore[attr-defined, assignment]
    loop._enter_mailbox = lambda: None  # type: ignore[attr-defined, assignment]
    loop._select_mail_sub_tab = lambda **_kwargs: True  # type: ignore[assignment]
    loop._fleet_filter_looks_on = lambda: False  # type: ignore[assignment]
    loop._on_mail_list = lambda: True  # type: ignore[attr-defined, assignment]
    loop._close_mail = lambda: None  # type: ignore[attr-defined, assignment]
    loop._dump_frame = lambda *_a, **_k: None  # type: ignore[attr-defined, assignment]
    loop._mail_list_rows = lambda **_kwargs: [row]  # type: ignore[assignment]
    loop._scroll_mail_list = lambda *_a, **_k: False  # type: ignore[attr-defined, assignment]

    def _open(r, _visit):  # type: ignore[no-untyped-def]
        opened.append(r.index)
        return False

    loop._open_mail_row = _open  # type: ignore[attr-defined, assignment]
    loop._scan_mail_rows(
        wanted=pirate_loop.ReportKind.RECYCLE,
        label="回收报告",
        visit=lambda _r, _p: False,
        skip_read_unknowns=skip_read_unknowns,
        max_pages=1,
    )
    return bool(opened)


def test_only_positively_read_unknowns_are_skipped() -> None:
    """⚠️⚠️ **三档处置,搞反任何一档都会让回收报告读不出来。**

    而「读不出来」在日志上和「信箱里没有回收报告」长得一模一样 ——
    2026-09-14 已经因此烧掉一整晚：`#339` 的第一版把**所有**主题读不出的行都跳过，
    实测那是 **87%** 的开封（昨夜 601 封里 522 封），回收报告正在其中，
    上线后回收读信当场变成每趟 0 份。
    """
    assert _opens_with(True, skip_read_unknowns=True) is True, "未读的必须开"
    assert _opens_with(None, skip_read_unknowns=True) is True, (
        "颜色读不出的必须开 —— `MailRow.unread` 的注释写明「读不出绝不能往已读那侧倒」"
    )
    assert _opens_with(False, skip_read_unknowns=True) is False, "确凿已读的才跳过"

    # 闸没开的那一趟（战报侧）：三档都照开，一封都不许漏。
    for state in (True, None, False):
        assert _opens_with(state, skip_read_unknowns=False) is True, (
            "战报那一侧漏开一封就是永久丢一份报告"
        )


def test_a_failed_subtab_check_logs_every_row_subject() -> None:
    """⚠️⚠️ **判据对不上时，要把那几行的主题原文打进日志，不能只打计数。**

    2026-09-14 晚：这一档连着 10 次都是「舰队类 N 行、非舰队类 1 行」，
    于是切不到「舰队」、回收读信每趟 0 份。而光看计数**分不出**两种相反的解释 ——
    「筛选开着但有一行读花」还是「筛选根本没切过去」。

    ⚠️ 那一档**存过现场图**，但图落在跑生产的那台机器上，排障的人未必够得到
    （当晚生产在 `CY-202305011401`，排障在另一台）。**日志是唯一一定拿得到的。**
    """
    lines: list[str] = []

    class Row:
        def __init__(self, index: int, kind: object, subject: str) -> None:
            self.index, self.kind, self.subject = index, kind, subject

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._mail_list_rows = lambda **_kwargs: [  # type: ignore[assignment]
        Row(0, pirate_loop.ReportKind.FLEET_RETURN, "eS  舰队返回"),
        Row(1, pirate_loop.ReportKind.ATTACK, "AN SO 攻击报告 iva"),
        Row(2, pirate_loop.ReportKind.UNKNOWN, "~~ — —w————"),
    ]

    import unittest.mock

    with unittest.mock.patch.object(pirate_loop, "say", lines.append):
        assert loop._sub_tab_matches(fleet=True, name="舰队", quiet=False) is False

    joined = "\n".join(lines)
    for subject in ("eS  舰队返回", "AN SO 攻击报告 iva", "~~ — —w————"):
        assert subject in joined, (
            f"判据失败时没把主题原文 {subject!r} 打进日志 —— 光有计数分不出「读花了」和「没切过去」"
        )
    assert "ATTACK" in joined, "没打出每一行判成了什么 kind"

    # ⚠️ `quiet=True` 那一次是「点之前先看一眼」，不匹配是**正常**的，不许刷屏。
    lines.clear()
    with unittest.mock.patch.object(pirate_loop, "say", lines.append):
        loop._sub_tab_matches(fleet=True, name="舰队", quiet=True)
    assert lines == [], "先看一眼那一次不该打日志（它不匹配是正常的）"


def test_the_fleet_tab_kinds_match_what_the_parser_documents() -> None:
    """⚠️⚠️ **「舰队标签里有哪几种信」这件事，仓库里不许有两个版本。**

    2026-09-14 晚实机：`FLEET_TAB_KINDS` 只列了两种（回收报告 / 舰队返回），
    而 `vision.parsers` 那段注释写着四种（实拍 2026-09-13：还有**矮星系统战报**
    与**部署报告**）。判据用的是错的那一份。

    代价：舰队标签第 0 行常驻一封矮星系统战报（最新那封）⇒「非舰队类 = 1」⇒
    `other == 0` 不成立 ⇒ **6 趟里 4 趟切不到「舰队」、回收读信每趟 0 份**，
    而当时库里有 325 发回收在等实收。

    ⚠️ 这一条把两处绑在一起：`parsers` 那段注释是实拍结论，它变了这里就要变。
    """
    assert set(pirate_loop.PirateLoop.FLEET_TAB_KINDS) == {
        pirate_loop.ReportKind.RECYCLE,
        pirate_loop.ReportKind.FLEET_RETURN,
        pirate_loop.ReportKind.STARGATE,
        pirate_loop.ReportKind.DEPLOY,
    }, "舰队标签的主题清单和 parsers 那段实拍注释对不上了"

    # 反向：这四种必须都被分类器认得出来，否则它们落 UNKNOWN，
    # 而 `may_be` 对 UNKNOWN 一律放行 —— 那是 parsers 那段注释自己的理由。
    from evo_helper.vision.parsers import classify_report_subject

    for text_, expected in (
        ("回收报告", pirate_loop.ReportKind.RECYCLE),
        ("舰队返回", pirate_loop.ReportKind.FLEET_RETURN),
        ("矮星系统战报", pirate_loop.ReportKind.STARGATE),
        ("部署报告", pirate_loop.ReportKind.DEPLOY),
    ):
        assert classify_report_subject(text_) is expected, f"{text_} 认不出来了"


def test_a_stargate_row_no_longer_blocks_the_fleet_tab_check() -> None:
    """⚠️ 实机那一屏的原样回放：第 0 行矮星系统战报 + 5 行舰队返回。

    改表之前这一屏判不成立（`other=1`），整趟放弃；改表之后它就是「筛选开着」。
    """
    F = pirate_loop.ReportKind.FLEET_RETURN
    S = pirate_loop.ReportKind.STARGATE

    assert _judge([S, F, F, F, F, F], fleet=True) is True, (
        "第 0 行那封矮星系统战报又把「舰队」确认拦掉了 —— 6 趟里 4 趟就是这么废掉的"
    )
    assert _judge([S, F, F, F, F, F], fleet=False) is False

    # ⚠️ 但确凿的攻击报告仍旧要拦住：那一屏是**没筛过**的列表。
    A = pirate_loop.ReportKind.ATTACK
    assert _judge([S, A, F, A, A, A], fleet=True) is False, "混着攻击报告的屏幕被判成「筛选开着」了"


def test_an_empty_filter_list_confirms_nothing() -> None:
    """⚠️⚠️ **「没有符合当前筛选条件的邮件」那一屏，什么都确认不了。**

    2026-09-15 01:04 实拍（`dump-mail-detail-unrendered-010442.png`）：信箱停在一个
    没有邮件的筛选档位上，那句话被当成「第 0 行的主题」读进来，整屏读成
    0 行舰队类、0 行非舰队类 —— 而 `good = not on` 在这种读数下**成立**。

    于是代码报告「筛选关掉了」，接着在空列表上一行行开幻影邮件，每封 ~24 秒、
    全部以「点开之后没读到「消息」标题」告终。**整轮卡死：启动 8 分钟里
    攻击 0 发、回收 0 发、战报 0 份。**
    """

    class Row:
        def __init__(self, subject: str) -> None:
            self.index, self.kind, self.subject = 0, pirate_loop.ReportKind.UNKNOWN, subject

    # 判据函数本身
    assert pirate_loop.mail_list_is_empty([Row("没有符合当前筛选条件的邮件。")]) is True
    # ⚠️ OCR 会把后半句读花（实拍读成「短选」），所以只认前六个字
    assert pirate_loop.mail_list_is_empty([Row("没有符合当前短选条件的邮件。")]) is True
    assert pirate_loop.mail_list_is_empty([Row("eS  舰队返回")]) is False
    assert pirate_loop.mail_list_is_empty([]) is False

    # 切标签那道判据：两个方向都不许在空列表上给出答案
    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._mail_list_rows = lambda **_kwargs: [  # type: ignore[assignment]
        Row("没有符合当前短选条件的邮件。")
    ]
    for fleet in (True, False):
        assert loop._sub_tab_matches(fleet=fleet, name="x", quiet=True) is False, (
            f"空列表被当成了「{'舰队开着' if fleet else '筛选关掉了'}」的证据"
        )


def test_the_scan_gives_up_on_an_empty_list_instead_of_opening_ghosts() -> None:
    """⚠️ 看见空列表就收手，**不许在上面开幻影邮件**。

    ⚠️ 而且要走 `aborted=True`：这一趟一封都没看过，不能被记成「翻完了」。
    """
    opened: list[int] = []

    class Row:
        index, kind = 0, pirate_loop.ReportKind.UNKNOWN
        subject = "没有符合当前短选条件的邮件。"
        raw_time_text = ""
        reported_at_utc = None
        unread = None

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._ensure_geometry = lambda: None  # type: ignore[attr-defined, assignment]
    loop._enter_mailbox = lambda: None  # type: ignore[attr-defined, assignment]
    loop._select_mail_sub_tab = lambda **_kwargs: True  # type: ignore[assignment]
    loop._fleet_filter_looks_on = lambda: False  # type: ignore[assignment]
    loop._on_mail_list = lambda: True  # type: ignore[attr-defined, assignment]
    loop._close_mail = lambda: None  # type: ignore[attr-defined, assignment]
    loop._dump_frame = lambda *_a, **_k: None  # type: ignore[attr-defined, assignment]
    loop._mail_list_rows = lambda **_kwargs: [Row()]  # type: ignore[assignment]
    loop._open_mail_row = lambda r, _v: opened.append(r.index) or False  # type: ignore[attr-defined, assignment]

    scan = loop._scan_mail_rows(
        wanted=pirate_loop.ReportKind.ATTACK,
        label="攻击战报",
        visit=lambda _r, _p: False,
    )

    assert opened == [], "在空列表上开了幻影邮件 —— 每封 ~24 秒，整轮会卡死"
    assert scan.aborted is True, "空列表那一趟没标 aborted，会被记成「翻完了」"
    assert scan.cut_short, "放弃了却没说理由"


def test_a_screen_with_no_readable_times_is_not_a_mail_list() -> None:
    """⚠️⚠️ **一行可解析的时刻都没有 ⇒ 这一屏不是邮件列表。**

    2026-09-15 01:03 实机：`已重新登录` 之后 **7 秒**就开工读信，界面还没画出来，
    于是在登录页上一行行开「幻影邮件」—— 每封 ~24 秒、全部以
    「点开之后没读到「消息」标题」告终，**整轮卡死：12 分钟里攻击 0、回收 0、战报 0**。

    读出来的「主题」暴露了那是什么页面：

        第 4 行 '7   |   kucleer@126.com>   he Vo»'
        第 5 行 '一 aa = 息EV-T GRION 1 / Kucleer \ ='

    ⚠️⚠️ **判据必须是「时刻」而不是「主题」**：主题读不出是**常态**
    （实测 87%），那些行照样是真邮件、照样要开。真邮件行长这样 ——
    「时刻读得出、主题是乱码」：`13/09/2026 21:08:34 'bad 1 are ”公克RED'`。
    """
    unrendered = pirate_loop.mail_list_looks_unrendered

    class Row:
        def __init__(self, at: object) -> None:
            self.reported_at_utc = at

    from datetime import UTC, datetime

    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)

    assert unrendered([Row(None), Row(None), Row(None)]) is True, "整屏没时刻，该判成不是列表"
    assert unrendered([Row(None), Row(now), Row(None)]) is False, (
        "只要有一行读得出时刻，它就是真列表 —— 其余行主题读不出是常态，不许因此放弃"
    )
    assert unrendered([]) is False, "空列表另有判据（`mail_list_is_empty`），不归这一条管"


def test_the_scan_gives_up_when_the_screen_is_not_a_mail_list() -> None:
    """⚠️ 不是列表就收手，别开幻影邮件；而且要标 `aborted`。"""
    opened: list[int] = []

    class Row:
        index, kind = 0, pirate_loop.ReportKind.UNKNOWN
        subject = "7   |   kucleer@126.com>   he Vo»"
        raw_time_text = ""
        reported_at_utc = None
        unread = None

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._ensure_geometry = lambda: None  # type: ignore[attr-defined, assignment]
    loop._enter_mailbox = lambda: None  # type: ignore[attr-defined, assignment]
    loop._select_mail_sub_tab = lambda **_kwargs: True  # type: ignore[assignment]
    loop._fleet_filter_looks_on = lambda: False  # type: ignore[assignment]
    loop._on_mail_list = lambda: True  # type: ignore[attr-defined, assignment]
    loop._close_mail = lambda: None  # type: ignore[attr-defined, assignment]
    loop._dump_frame = lambda *_a, **_k: None  # type: ignore[attr-defined, assignment]
    loop._mail_list_rows = lambda **_kwargs: [Row()]  # type: ignore[assignment]
    loop._open_mail_row = lambda r, _v: opened.append(r.index) or False  # type: ignore[attr-defined, assignment]

    scan = loop._scan_mail_rows(
        wanted=pirate_loop.ReportKind.ATTACK,
        label="攻击战报",
        visit=lambda _r, _p: False,
    )

    assert opened == [], "在不是邮件列表的画面上开了幻影邮件"
    assert scan.aborted is True and scan.cut_short


def test_the_check_reads_again_while_the_list_refreshes() -> None:
    """⚠️⚠️ **点完标签之后要多看几次，不能只看一次就判失败。**

    2026-09-15 02:31 实拍（`dump-mail-sub-tab-战斗-unconfirmed-023122.png`）：
    点「战斗」之后判据连着读到的还是**舰队类**，于是判定失败、又点了一次
    （很可能把刚切好的点回去）；而失败时存下来的现场图上，列表第一行赫然是
    **「攻击报告」**、角标 15 —— **那一下其实成功了，只是列表刷新比判据慢**。

    ⚠️ `#336` 当年「战斗那个按钮不响应」的结论多半也是同一个时序假象，
    而那个错误结论让「切回战报」整整走错了两天。

    这一条钉住：**一次读不到，要再等再读，不许立刻重点**。
    """
    reads: list[int] = []
    clicks: list[str] = []

    class Driver:
        def click(self, _x: int, _y: int, *, label: str = "") -> None:
            clicks.append(label)

        def wait(self, _seconds: float) -> None:
            pass

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._driver = Driver()  # type: ignore[attr-defined]
    loop._on_mail_list = lambda: True  # type: ignore[attr-defined, assignment]
    loop._dump_frame = lambda *_a, **_k: None  # type: ignore[attr-defined, assignment]

    # 前 3 次读到的还是旧内容（没刷新），第 4 次才换过来。
    def _matches(**_kwargs: object) -> bool:
        reads.append(1)
        return len(reads) >= 4

    loop._sub_tab_matches = _matches  # type: ignore[assignment]

    assert loop._select_mail_sub_tab(fleet=False) is True, (
        "列表刷新慢了几拍就判失败了 —— 那正是攻击战报整晚读不到的成因"
    )
    assert len(clicks) == 1, f"点了 {len(clicks)} 次；一次没读到就重点，很可能把刚切好的又点回去"


# -- 2026-09-15 早「45 趟里 15 趟白跑」教出来的 -------------------------------


def test_a_half_repainted_screen_is_a_reason_to_wait_not_to_click_again() -> None:
    """⚠️⚠️ **一屏里两种都有 ⇒ 列表还在重绘,只等,不许再点。**

    实拍 2026-09-15 06:52(`dump-mail-sub-tab-舰队-unconfirmed-065255.png` 那一趟):
    点完「舰队」之后连着两次读到「舰队类 5 行、非舰队类 1 行」——**那就是舰队列表**,
    只是混进一行还没重绘完的「攻击报告」。判据要求 `other == 0`,于是判成不对、
    **又点了一次**,而再点就是把档位点掉,最后停在战斗档位上收手。
    整夜 45 趟回收里 **15 趟(33%)** 就是这样白跑的。

    ⚠️ 这一条钉的是**动作**,不是判据:`on = here > 0 and other == 0` 一个字没动。
    """
    kind = pirate_loop.ReportKind
    mixed = [kind.RECYCLE] * 5 + [kind.ATTACK]
    clean = [kind.RECYCLE] * 6
    screens = [mixed, mixed, clean]
    clicks: list[str] = []

    class Driver:
        def click(self, _x: int, _y: int, *, label: str = "") -> None:
            clicks.append(label)

        def wait(self, _seconds: float) -> None:
            pass

    class Row:
        def __init__(self, index: int, kind: object) -> None:
            self.index, self.kind, self.subject = index, kind, "某封信"

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._driver = Driver()  # type: ignore[attr-defined]
    loop._on_mail_list = lambda: True  # type: ignore[attr-defined, assignment]
    loop._dump_frame = lambda *_a, **_k: None  # type: ignore[attr-defined, assignment]
    loop._mail_list_rows = lambda **_k: [  # type: ignore[assignment]
        Row(i, k) for i, k in enumerate(screens.pop(0) if screens else clean)
    ]

    assert loop._select_mail_sub_tab(fleet=True) is True, (
        "重绘中的那一帧被判成了「切错了」—— 那正是 15 趟回收白跑的成因"
    )
    assert clicks == [], f"在重绘中的画面上点了 {len(clicks)} 次;再点一下就是把切好的档位点掉"


def test_a_screen_with_no_fleet_rows_at_all_still_gets_clicked() -> None:
    """⚠️ 「只等不点」**只适用于混合帧**,别让它变成不点的万能借口。

    `here == 0`(一行舰队类都没有)是真的没切过去 —— 问「关掉了?」的那 16 行
    全落在这一档。这一档照旧该点。
    """
    kind = pirate_loop.ReportKind
    screens = [[kind.ATTACK] * 6, [kind.RECYCLE] * 6]
    clicks: list[str] = []

    class Driver:
        def click(self, _x: int, _y: int, *, label: str = "") -> None:
            clicks.append(label)

        def wait(self, _seconds: float) -> None:
            pass

    class Row:
        def __init__(self, index: int, kind: object) -> None:
            self.index, self.kind, self.subject = index, kind, "某封信"

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._driver = Driver()  # type: ignore[attr-defined]
    loop._on_mail_list = lambda: True  # type: ignore[attr-defined, assignment]
    loop._dump_frame = lambda *_a, **_k: None  # type: ignore[attr-defined, assignment]
    loop._mail_list_rows = lambda **_k: [  # type: ignore[assignment]
        Row(i, k) for i, k in enumerate(screens.pop(0) if screens else [kind.RECYCLE] * 6)
    ]

    assert loop._select_mail_sub_tab(fleet=True) is True
    assert len(clicks) == 1, f"一行舰队类都没有还不点,点了 {len(clicks)} 次"


def test_what_counts_as_a_half_repainted_screen() -> None:
    """判据本身的真值表,三档都钉住。"""
    assert pirate_loop.sub_tab_frame_is_mid_repaint(5, 1) is True, "两种都有 = 还在重绘"
    assert pirate_loop.sub_tab_frame_is_mid_repaint(6, 0) is False, "干净的舰队屏不是重绘中"
    assert pirate_loop.sub_tab_frame_is_mid_repaint(0, 6) is False, (
        "一行舰队类都没有 = 真的切错了,这一档要照旧去点,不是等"
    )


def test_a_mixed_screen_that_never_settles_still_gets_clicked() -> None:
    """⚠️⚠️⚠️ **等待不能取代点击。** `#350` 原样上生产时这里是个必败的坑。

    2026-09-15 07:24 `ceb5de6` 上生产,第一趟回收:三次 attempt 全读到混合帧、
    于是**一次都没点**,干等 106 秒放弃。而那两行根本不是重绘残影 ——
    100 秒里读数一字未变(`'28 攻击报告 bad'` / `'人人》 攻击报告 SX'`),
    那是一屏**稳定的真实画面**(筛选没选上,列表混着各类信)。

    混合帧只说明「两种都在」,**推不出「它是瞬态的」**。
    等一等是对的(真瞬态时能省掉一次危险的重点),但等不到就必须照常点 ——
    点一下恰恰是这一档唯一能救回来的动作。
    """
    kind = pirate_loop.ReportKind
    mixed = [kind.RECYCLE] * 4 + [kind.ATTACK] * 2
    clean = [kind.RECYCLE] * 6
    clicked: list[str] = []

    class Driver:
        def click(self, _x: int, _y: int, *, label: str = "") -> None:
            clicked.append(label)

        def wait(self, _seconds: float) -> None:
            pass

    class Row:
        def __init__(self, index: int, kind: object) -> None:
            self.index, self.kind, self.subject = index, kind, "某封信"

    loop = pirate_loop.PirateLoop.__new__(pirate_loop.PirateLoop)
    loop._driver = Driver()  # type: ignore[attr-defined]
    loop._on_mail_list = lambda: True  # type: ignore[attr-defined, assignment]
    loop._dump_frame = lambda *_a, **_k: None  # type: ignore[attr-defined, assignment]
    # ⚠️ 点之前永远是那一屏混合的，点完才换过来 —— 「等」在这种屏上永远等不到。
    loop._mail_list_rows = lambda **_k: [  # type: ignore[assignment]
        Row(i, k) for i, k in enumerate(clean if clicked else mixed)
    ]

    assert loop._select_mail_sub_tab(fleet=True) is True, (
        "一直是混合帧就再也不点了 —— 那正是 ceb5de6 上生产后第一趟回收干等 106 秒的死法"
    )
    assert clicked, "从头到尾一次都没点；等待把唯一能救回来的动作顶掉了"
