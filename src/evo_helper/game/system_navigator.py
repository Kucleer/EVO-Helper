"""恒星系导航栏：把坐标输进去，让行星详情面板显示出来。

实测出来的两条行为，改这里之前先读一遍：

- **点任一输入框会弹出覆盖整条导航栏的编辑浮层**，所以一次只能设一个字段：
  点字段 → 输入 → 点 OK → 再设下一个。一口气点两个字段，第二个数字会进第一个字段。
- **`«` / `»` 是切换恒星系，不是翻页**，而且会关掉详情面板回到恒星系视图。
  扫描不用它：恒星系视图是一张可平移的散点图，行星没有位号标签，
  看不出「这一屏是不是 20 个位都看到了」——覆盖度无法自证，会留下静默缺口。

导航是只读操作，但仍然走 `HumanInput`：固定节奏的点击是最明显的自动化特征。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from evo_helper.domain.models import Coordinate

#: 导航栏三个输入框与 OK（截图 client 空间，标定视口 1920×879 + 38px 标题栏）。
GALAXY_FIELD = (795, 71)
SYSTEM_FIELD = (959, 71)
POSITION_FIELD = (1122, 71)
OK_BUTTON = (1029, 132)

#: 切视图：点底部左侧的星球按钮弹出子菜单，子菜单里**只列出你现在不在的那些视图**。
#: 游戏会自己回到行星视图（会话刷新时实测发生过），所以这条路径是必需的，不是可选的。
VIEW_MENU_BUTTON = (749, 856)

#: 子菜单第二项（带环行星图标）。**同一个像素，含义随当前视图而变**：
#: 在行星地表上点它 → 回恒星系视图；在恒星系视图上点它 → 去行星地表。
#: （第一项是银河系视图，图标是螺旋。）
#:
#: 所以这个常量有两个名字，指向同一处：按当前在哪来读它，别按名字想当然。
#:
#: 用户 2026-08-09 指出：**回星球地表**要走这个菜单，不要走底部导航的「行星」——
#: 那个开出来的是行星列表浮层，一排八个图标全是真实操作（运输/部署/传送/转移…）。
#:
#: ⚠️ 那句话只管「回地表」这一件事，不是说那个浮层用不上。**切换当前星球恰恰
#: 只能走它**：浮层里每一行的「前往此处」是全仓唯一一条换出发星球的路
#: （`game.planet_list`）。同一个按钮、两个目标、结论相反，见
#: `game.pirate_ui.NAV_PLANET` 那一段，两种用途在那里写全了。
SYSTEM_VIEW_BUTTON = (737, 748)
PLANET_VIEW_BUTTON = SYSTEM_VIEW_BUTTON

#: 导航栏三个字段的**标签**所在的横条。行星视图上这里是空的，可以用来判断在不在恒星系视图。
NAV_LABEL_ROI = (740, 88, 1190, 115)
NAV_LABELS = ("银河系", "恒星系", "行星")

#: 导航栏三个**值框**（银河系 / 恒星系 / 行星），也就是 `_set` 往里打字的那三个框。
#:
#: ⚠️ **这不是偏好项。** 取值由屏幕几何决定：框位量自实拍 `var/logs/calib-恒星系-client.png`
#: （1920×917 client 空间，与 `GALAXY_FIELD` 等三个点击点同一次标定），值那一行就压在
#: `NAV_LABEL_ROI` 正上方。窗口尺寸或界面版面变了要重量，改成别的数只会读错。
#:
#: 注意与 `NAV_LABEL_ROI` 的分工：那条读的是**标签**（用来判断在不在恒星系视图），
#: 这三条读的是**值**（用来判断导航栏此刻停在哪个坐标）。
#: 下界与 `NAV_LABEL_ROI` 的上界**贴齐但不重叠**：往下多框两行就把「银河系」那几个
#: 中文挤进了数字白名单，读出来是噪声——而噪声与空串在这条链路上不是一回事。
NAV_VALUE_ROIS = ((730, 55, 865, 88), (893, 55, 1028, 88), (1056, 55, 1191, 88))

#: 读值框的配方 `(放大倍数, 二值化阈值, 是否裁到墨迹外框)`。**每个框各自**跑完
#: **全部**配方，再由 `agreed_value` 汇总——不是「读出非空就采纳」，理由见下。
#:
#: ⚠️ 同样是标定常量，不是偏好项：取值由字号、二值化门槛、Tesseract 的行为决定，
#: 调了不会「更适合我」，只会读错。
#:
#: ## 上一版那句「只会读空不会读错」被实机否掉了
#:
#: 原先这里是 `((3, 170), (2, 140), (2, 170))` + 「第一套读出非空就采纳」，注释写着
#: 那三套「只会读空，不会读错」，依据是九张实拍上「八张全对、零张读错」。
#: **实机把这句话推翻了**：生产 `system_log` 2026-08-18，这条回读上线以来
#: **28 次回读、28 次对不上，成功率 0%**，错法一律是丢掉最左那一位——
#: `277` 读成 `77`、`250` 读成 `50`、`15` 读成 `5`。
#:
#: 那次标定错在**语料没有覆盖会出错的字形**：九张里八张的恒星系框都是 `137`，
#: 而 `137` 是这套字体里最结实的一个数——每套配方都读得对。语料里压根没有
#: 「首位是 2 的多位数」，于是「不会读错」这个结论从来没被考验过。
#:
#: 2026-08-18 在 43 张实拍（129 个值框，真值逐张目视核对）上重扫，
#: 老配方 + 老规则的成绩是：**三格全对 35/43，但有 3 个格子读错**——
#: `27` 读成 `7`、`52` 读成 `5`、`9` 读成 `3`。前两个和生产那三条日志是同一种错法。
#: 也就是说这个缺陷在本机实拍上**是能复现的**，只是原来那九张里没有。
#:
#: ## 为什么是「裁到墨迹外框 + 低倍放大」
#:
#: 二值化后的图是干净的——`52` 在每套配方下都清晰可辨，所以既不是 ROI 切了、
#: 也不是阈值吃了笔画。丢位发生在 **Tesseract 那一步**：值框是 135×33 的一大片黑，
#: 数字只占中间一小块，`--psm 7`（单行）在这种「小字浮在大片空白里」的图上会漏字。
#: 先裁到墨迹外框、再补一圈边距（`tight`），把字撑满画面，丢位就基本消失；
#: 而放大倍数**不是越大越好**：原字高约 14px，2× 落在 Tesseract 最舒服的字号上，
#: 3×／4× 反而把字撑过头（老配方全是 3×／4×，这正是它们丢位的原因）。
#:
#: 这五套是从 18 套候选里挑的，判据是「在 43 张实拍上一个格子都不许读错」，
#: 且**每一套单独看错法只有丢位**（不替换、不凭空多一位）——`agreed_value` 的
#: 安全性就架在这条性质上，所以它由 `tests/integration/vision/test_nav_bar_values_live.py`
#: 逐套守着。含替换／多位错法的配方进池会把汇总规则带崩（实测把 18 套全塞进去，
#: 读错格从 0 涨到 9）。
NAV_VALUE_RECIPES = (
    (1, 170, True),
    (2, 140, True),
    (2, 200, True),
    (3, 170, True),
    (2, 170, False),
)

#: 一个值框的读数要被采纳，至少要有这么多套配方读出**同一个**值。
#:
#: ⚠️ 标定常量，不是偏好项——它是 `agreed_value` 安全论证的一半。压到 1 就退回
#: 「一套配方说了算」，也就是这次修的那个缺陷本身（老规则的「首个非空」等价于
#: 一票通过）；抬到 3 以上则会把「只有两套读得出、其余读空」的正常情形判成读不出。
NAV_VALUE_MIN_VOTES = 2

#: 点开字段后等编辑浮层弹出。
FIELD_OPEN_WAIT_S = 0.7

#: 点 OK 后等画面切换。切屏时间不稳定，这是下限而不是精确值。
APPLY_WAIT_S = 2.6

#: 切视图后等画面切换。
VIEW_SWITCH_WAIT_S = 2.0


def _is_dropped_from(shorter: str, longer: str) -> bool:
    """`shorter` 是不是 `longer` 删掉若干位之后的样子（子序列，且真的更短）？

    这正是实测到的唯一错法：配方会**漏掉**一位（`277`→`77`、`52`→`5`、`11`→`1`），
    不会把某一位读成另一个数、也不会凭空多出一位。所以「短的那个是长的那个的
    子序列」就等于「这两个读数的分歧可以用漏字解释」。
    """
    if len(shorter) >= len(longer):
        return False
    remaining = iter(longer)
    return all(character in remaining for character in shorter)


def agreed_value(reads: Sequence[str], *, min_votes: int = NAV_VALUE_MIN_VOTES) -> str:
    """一个值框在各套配方下的读数，汇成一个能采纳的值；汇不拢就交空串。

    规矩三条，缺一不可：

    1. **至少 `min_votes` 套配方读出同一个值**，那个值才有资格。一票不算——
       这就是这次修的那个缺陷：老规则「第一套读出非空就采纳」等于一票通过，
       于是 `(3,170)` 把 `277` 读成 `77`、非空、当场被采纳，后两套根本没机会跑。
    2. 够票的候选里**取最长的那个**。错法只有漏字（见 `_is_dropped_from`），
       所以更长的那个才是更完整的那个。
    3. 其余每一个非空读数都**必须能用漏字解释**（是胜出者的子序列）。解释不了就
       意味着出现了替换或凭空多位，那种分歧这里没有能力裁决 → 交空串。

    第 3 条顺带把「两个一样长却不同的候选都够票」也管掉了，所以这里**没有**单独一道
    歧义闸：漏字必然更短，等长的对手永远解释不通，于是必定在第 3 条上作废。
    原先真写过那道闸，变异测试证明它是死代码（拆掉一个用例都不红）——
    这个仓库不留没有判据守着的分支。

    ## 这条规则挡住了什么

    - `['93', '9', '9', '', '']`（一套凭空多一位）→ 空串。`93` 只有一票，当不了候选；
      而 `9` 当上候选之后 `93` 不是它的子序列，于是整格作废。**一套配方的臆造
      既赢不了，也不会被无声吞掉。**
    - `['77', '77', '277', '', '']`（真值可能是 `277`，但只有一套看全了）→ 空串。
      宁可退回三字段全设，也不认一个可能缺了位的值。
    - `['3', '9', '', '', '']`（替换）→ 空串。
    - `['9', '', '', '', '']`（只有一套读得出）→ 空串。

    仍然挡不住的：**两套配方犯同一个臆造**（`['93','93','9']` → `93`）。
    这是残余风险，不是被忽略的风险——池子里每一套配方「错法只有漏字」这条性质
    由实拍用例逐套守着，正是为了让这种情形没有配方可以依附。

    非数字读数（Tesseract 偶尔把白名单里的冒号也吐出来，比如 `'27 :'`）不参与投票：
    它不可能等于任何一个坐标分量，当读不出处理。**但调用方仍要把原文记进日志**——
    「读成了什么」是下次校准唯一的线索。
    """
    values = [text for text in reads if text.isdigit()]
    tally = Counter(values)
    candidates = [text for text, votes in tally.items() if votes >= min_votes]
    if not candidates:
        return ""
    best = max(candidates, key=len)
    if any(text != best and not _is_dropped_from(text, best) for text in values):
        return ""
    return best


def reads_like_a_dropped_digit(read: str, wanted: str) -> bool:
    """`read` 看起来是不是「`wanted` 漏了位」？**只用来给日志定性，不参与采纳。**

    回读对不上的时候，这一句区分的是两种完全不同的处境：漏位说明**画面是对的、
    是读错了**（生产 2026-08-18 那 28 次全是这一类，用户当场核对过「实际页面是
    切回去了」），而对不上又不像漏位，才是「导航栏真的不在这个坐标上」。

    ⚠️ **绝不许拿它去放行采纳。** 「疑似漏位」离「就是这个坐标」还差得远，而认错
    一次的代价是缓存与导航栏分岔（见 `SystemNavigator` 类注释里 136→9 那次事故）。
    """
    return bool(read) and _is_dropped_from(read, wanted)


def on_system_view(text: str) -> bool:
    """导航栏标签读到两个以上就算在恒星系视图。

    要两个而不是一个：`行星` 在底部导航条上也有，单个标签命中不足以证明在哪一屏。
    """
    return sum(label in text for label in NAV_LABELS) >= 2


class ScanDriver(Protocol):
    """扫描需要的最小操作面。真实实现驱动鼠标，测试里换成假的。"""

    def click(self, x: int, y: int, *, label: str = ...) -> None: ...

    def type_number(self, value: int) -> None: ...

    def capture(self) -> Any: ...

    def wait(self, seconds: float) -> None: ...


@dataclass
class SystemNavigator:
    """按坐标导航，并记住当前停在哪，好省掉不必要的字段输入。

    银河系和恒星系只在变化时才重设——**一个字段实测 6.6 秒**，
    同一恒星系内连扫 16 个位时省下的时间占了大头。

    6.6 秒是从生产 `system_log` 量出来的（2026-08-17 一天 177 次派遣）：导航耗时
    分成两簇，设两个字段 150 次均值 14.02 秒、设三个字段 37 次均值 20.65 秒。
    （这里原先写的是「约 3 秒」，那是按 `FIELD_OPEN_WAIT_S + APPLY_WAIT_S` 估的，
    实测翻了一倍——点击与画面切换的真实耗时不在那两个常量里。）

    ## 缓存里只放**回读确认过**的坐标

    `current` 不是「我刚才往框里打了什么」，而是「面板回读证明导航栏就是这个坐标」。
    打完字不算数，要调用方拿到证据之后调 `confirm()` 才算——`goto()` 结束时先把
    `current` 清空。

    这个区分是有代价才换来的。实机 2026-08-11：一次「设恒星系」落到了银河系框上，
    游戏把 136 截断成最大值 9，此后导航栏是 `[9:137:x]` 而缓存说 `2:137`；判「一样」
    用的就是那份错记忆，于是银河系再也不会被重设，连续 44 个目标坐标核对全不过。
    按「打了什么」记，那份记忆本身就可能是错的；按「读回来什么」记，错的记不进来。

    于是省字段这件事从「假定」变成了「有证据」：
    调用方每次导航之后都回读面板坐标行（`vision.scan_reading.read_panel_confirming`
    是那份唯一的判据），读通了就 `confirm()`，读不通就什么都不记——下一趟自然会把
    三个字段都重设一遍。**方向永远是「拿不准就多设」**，绝不会因为省事而少设。

    反过来，正因为缓存有证据撑着，关掉浮层（派遣面板、信箱）之后不必再无条件
    `invalidate()`：那些浮层压根不改导航栏的值，而真改了值的那些动作（切视图、
    重连、关窗重开）仍然照旧清缓存，见 `invalidate()` 的调用点。

    切换出发星球是「真改了值」那一类，但它有别的出路：切完之后
    `tools.pirate_loop.ensure_origin_planet` 回读导航栏的三个值框，读通了就
    `adopt_readback()`。**这仍然是回读确认，不是假定**——读的正是缓存所描述的
    那三个框本身，读不通照样 `invalidate()`。
    """

    driver: ScanDriver
    #: **回读确认过**的坐标。None 表示不确定，下一次会把三个字段都设一遍。
    current: Coordinate | None = None

    def goto(self, coordinate: Coordinate) -> None:
        at = self.current
        # 位置永远重设：它是本次要读的那个字段，不能靠推断。
        if at is None or at.galaxy != coordinate.galaxy:
            self._set(GALAXY_FIELD, coordinate.galaxy, "银河系")
        if at is None or at.system != coordinate.system:
            self._set(SYSTEM_FIELD, coordinate.system, "恒星系")
        self._set(POSITION_FIELD, coordinate.position, "行星")
        # ⚠️ **打完字不等于跳过去了。** 这里必须清空而不是记下 `coordinate`：
        # 数字可能进错框（实机上 136 就这么被截成 9）、可能压根没进去。
        # 记忆要等 `confirm()`，也就是等面板回读给出证据。
        self.current = None

    def confirm(self, coordinate: Coordinate) -> None:
        """回读确认：面板读回来的就是这个坐标，导航栏可以据此省字段了。

        **只由拿到证据的那一方调用。** 证据是行星详情面板上的坐标行——
        `vision.scan_reading.read_panel_confirming` 逐套配方读到核对通过为止，
        那是全仓唯一一份「面板读出来的算不算数」的判据。
        """
        self.current = coordinate

    def adopt_readback(self, coordinate: Coordinate, values: Sequence[str]) -> bool:
        """导航栏三个值框读回来就是 `coordinate` 才记住它，否则清缓存。返回记没记住。

        这是 `confirm()` / `invalidate()` 之外**唯一**该被外面调的入口，因为它把
        「读通了才认」这条规矩关在了里面：调用方交出去的是三个**读数**，不是一个
        「我觉得现在在哪」的判断，所以不存在「忘了检查就 confirm」这条路。

        读不出（任何一个框是空串）与读出别的坐标，两种都走 `invalidate()`：
        **方向永远是「拿不准就多设」**，理由整段在类注释里那次 136→9 的事故。
        """
        if _reads_as(values, coordinate):
            self.confirm(coordinate)
            return True
        self.invalidate()
        return False

    def invalidate(self) -> None:
        """画面被别的东西改过（切视图、重连、关窗重开）之后调用，下一次重设全部字段。

        ⚠️ 只用在**导航栏本身可能变了**的地方。单纯关掉一层浮层不属于这一类：
        浮层不改导航栏的值，而清掉一份回读确认过的记忆，代价是下一个目标白设
        两个字段（约 6 秒）——海盗那条链路每颗星球都要付一次。
        """
        self.current = None

    def ensure_system_view(self, read_nav_labels: Callable[[], str], *, attempts: int = 3) -> bool:
        """确保停在恒星系视图；不在就走「星球按钮 → 恒星系」把它切回来。

        判据是导航栏标签而不是画面差异——切视图的动画期间画面会变，但那不等于切到了。
        试满 `attempts` 次仍读不到标签就返回 False，由调用方停止：接着往
        `(795, 71)` 之类的固定坐标点下去，就是在认不出的画面上乱点。
        """
        for attempt in range(attempts):
            if on_system_view(read_nav_labels()):
                return True
            self.driver.click(*VIEW_MENU_BUTTON, label="视图菜单")
            self.driver.wait(1.0)
            self.driver.click(*SYSTEM_VIEW_BUTTON, label="恒星系视图")
            self.driver.wait(VIEW_SWITCH_WAIT_S * (attempt + 1))
            # 视图换过之后导航栏里是什么已经不可知了。
            self.invalidate()
        return on_system_view(read_nav_labels())

    def _set(self, field: tuple[int, int], value: int, label: str) -> None:
        self.driver.click(*field, label=label)
        self.driver.wait(FIELD_OPEN_WAIT_S)
        self.driver.type_number(value)
        self.driver.click(*OK_BUTTON, label="OK")
        self.driver.wait(APPLY_WAIT_S)


def _reads_as(values: Sequence[str], coordinate: Coordinate) -> bool:
    """三个读数**逐字**就是这个坐标吗？

    严格到「一个字都不许差」是有意的：读数是 OCR 的产物，任何「差不多」的放宽
    （去掉非数字、只比前缀、只比银河系）都会把一次误读放行成一份假记忆。
    宁可判不通——判不通只花两次字段输入，判错要付的是整轮坐标核对全不过。

    ⚠️ **行星那一格也要比**，哪怕 `goto()` 每次都会重设行星。它在这里的作用不是
    省字段，而是第三个互相独立的见证：三个格子同时误读成同一个坐标，比一个格子
    误读难得多。
    """
    if len(values) != 3:
        return False
    return tuple(values) == (
        str(coordinate.galaxy),
        str(coordinate.system),
        str(coordinate.position),
    )


def crop_reader(image: Any, ocr: Callable[..., str]) -> Callable[..., str]:
    """把一张截图包成 `scan_reading.read_panel` 要的取字函数。"""

    def read(box: tuple[int, int, int, int], **recipe: Any) -> str:
        # 配方原样透传：坐标行会带上 resample，名字行不带。
        return str(ocr(image.crop(box), **recipe))

    return read
