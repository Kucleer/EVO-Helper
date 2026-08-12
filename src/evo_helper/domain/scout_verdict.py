"""海盗侦察报告的判定规则：打、不打、还是没看清。

规则本身早就有（原先散在 `game.pirate_ui.triggers_attack` 与
`vision.scout_reports.PirateScoutReading.verdict` 两处），这里只是把它挪到
**domain 这一层**，因为它现在有了第三个消费者：仓储要按库里存下来的证据
回答「这个海盗走到哪一步了」（`domain.pirate_round`）。

挪的必要性不是洁癖。`storage` 不能 import `vision`，而 `vision.scout_reports`
里那份 `verdict` 是活链路当场用的那一份——两边各写一份判据，就会出现
「界面上说不值得打、而链路当时判的是没看清」这种谁也说不清的分叉。
`game.pirate_ui` 仍旧原样 re-export `PIRATE_TRIGGER_SHIPS` 与 `triggers_attack`，
调用方一个都不用改。

⚠️ **判定不落库。** `domain.records.ScoutReport` 的注释写着理由：门槛与舰种表
是会变的规则，把当时算出来的结论钉进库里，规则一改那一列就成了没人知道
是按哪版算的死数。库里存证据（每一格读到什么、哪几格没读出来），
要结论就把证据读回来、按**现行**规则算一遍。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from evo_helper.domain.records import ScoutReport

#: 判定门槛：侦察报告里这几种舰船任一数量超过这个值，就用攻击预设打。
#: 门槛低是用户明确确认过的——几乎每个有舰队的海盗都会命中。
PIRATE_TRIGGER_SHIPS = (
    "深空吞噬者",
    "噬能截击者",
    "钛能守卫者",
    "收割者",
)
PIRATE_TRIGGER_MIN_COUNT = 1


#: 判定结论。三值，不是布尔——「读不出来」必须和「读出来了，不值得打」分开：
#: 前者要重读或跳过，后者是已经问出答案了。合成一个布尔就分不出这两种。
VERDICT_ATTACK = "ATTACK"
VERDICT_SKIP = "SKIP"
VERDICT_UNREADABLE = "UNREADABLE"


def triggers_attack(ships: Mapping[str, int]) -> bool:
    """侦察到的舰队是否够格挨一发。

    「任一 > 1」是用户确认过的字面规则。注意这**不是**强弱判断——
    它只问「这个海盗有没有实打实的舰队」，不问打不打得过。
    """
    return any(ships.get(name, 0) > PIRATE_TRIGGER_MIN_COUNT for name in PIRATE_TRIGGER_SHIPS)


def verdict_for(counts: Mapping[str, int], *, unread: Iterable[str] = ()) -> str:
    """打、不打、还是不下结论。`counts` 只放**读出来了**的格子。

    判据的不对称性是有意的：

    - 读到任何一个 > 1 就打。这个方向的证据是**正面**的——那个数确实读出来了，
      缺的几格再怎么样也只会让舰队更强，不会让结论反过来。
    - 读到的都 ≤ 1、却还有没读出来的格子：**不下结论**。缺的那格可能正是
      一支实打实的舰队，当成 0 就会把「没看清」记成「这里是空的」。
    - 四格都读出来且都 ≤ 1：这才叫「不值得打」。

    ⚠️ **规则表里有、而 `counts` 里没有的舰种一律算「没读出来」**，
    不必由调用方自己补进 `unread`。活链路那边补得很齐
    （`vision.scout_reports.read_pirate_scout` 的 `missing` 正是这个集合，
    所以这一条对它是恒等的），但库里那份是**当时**读到的快照：规则表以后
    加一个舰种，旧报告里根本没有那一行，而「没有那一行」的真相是没看过，
    不是看过且为 0。少了这一条，加舰种的当天所有旧报告会集体翻成
    「不值得打」——正是本模块反复在防的那种把「没看清」记成「空的」。
    """
    if triggers_attack(counts):
        return VERDICT_ATTACK
    blind = set(unread) | {name for name in PIRATE_TRIGGER_SHIPS if name not in counts}
    return VERDICT_UNREADABLE if blind else VERDICT_SKIP


def verdict_of_record(report: ScoutReport) -> str:
    """库里那份侦察报告，按现行规则算下来是打还是不打。

    `count is None` 的格子就是「没读出来」（见 `domain.records.ScoutTriggerShip`），
    **不许在这里补成 0**。

    这条与 `application.report_ingest.to_scout_reading(...).verdict` 必须给出
    同一个答案——那条路是把记录还原成活链路的读数再问一次。两条路都归到
    `verdict_for`，所以它们不会分叉；测试里钉了这条等价。
    """
    counts = {
        entry.ship_type: entry.count for entry in report.trigger_ships if entry.count is not None
    }
    unread = tuple(entry.ship_type for entry in report.trigger_ships if entry.count is None)
    return verdict_for(counts, unread=unread)


__all__ = [
    "PIRATE_TRIGGER_MIN_COUNT",
    "PIRATE_TRIGGER_SHIPS",
    "VERDICT_ATTACK",
    "VERDICT_SKIP",
    "VERDICT_UNREADABLE",
    "triggers_attack",
    "verdict_for",
    "verdict_of_record",
]
