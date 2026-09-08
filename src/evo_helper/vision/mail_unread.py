"""信箱列表页「未读 / 已读」的颜色判据。**2026-09-08 已在实机实拍上标定。**

## 这一条领域事实是什么

用户口径（2026-09-08）：

> 「邮箱中未读邮件（前 4 个），字体颜色是与已读邮件不一致的，你在开未读邮件时，
> 需要把这些内容都阅读了。」

> 「注意 正常情况下 是连续的未读批量 而不是现在读的稀稀拉拉」

所以**正常版面是「顶部一段连续的未读」**；实拍上量到的稀疏分布（未读散在
24 / 30 / 36 / 72 行深处）是 `MAIL_MAX_OPENS` + 早停一趟趟留下的**历史空洞**，
不是常态。这条口径管着策略层的形状，整段在 `tools.pirate_loop.MAIL_UNREAD_MAX_OPENS`。

实拍量出来的（2026-09-08 四屏信箱列表页，27 行）：标题「攻击报告」四个字
**未读是黄橙（R−B ≈ +220），已读是白（R−B ≈ −5）**，中间隔着两百多。

⚠️ **「未读行整行底色略亮」那条观察是错的，已撤回。** 底色是整块面板的**渐变**：
实测已读第 3 行亮度 88 > 未读第 1 行 45。**标题色是唯一的信号**，`mean_luminance`
因此只剩诊断用途，见 `MailRowColor`。

## 为什么这件事值钱

未读是比「列表页时刻」**更强**的信号：

- 未读 ⇒ 按定义我们还没开过它 ⇒ 库里不可能有 ⇒ 一定要开；
- 已读 ⇒ 已经开过 ⇒ `_already_in_library` 那道时刻闸（#288）是对的。

#288 那道闸是拿「时刻 + 同秒行数」去**近似**这件事，而颜色是**直接**读到它。

## ⚠️ 量哪一块，比阈值取多少要紧得多

判据挂在**自对齐的标题带**上（`optional.report_screens.mail_row_colors`），
不是整行。三种取法在同一批 27 行样本上的实测：

    ROI 做法              面积        未读最小   已读最大   比值
    整行（44,200 px）     44,200 px   0.0153     0.0092     1.7×
    标题带，按名义行顶     1,296 px   0.0000     0.0000     分不开
    标题带，自对齐         1,296 px   0.2137     0.0054     40×

整行只有 1.7× 是因为黄字只有约 434 个墨迹像素，被 44,200 像素稀释掉了；
按名义行顶取会归零是因为滚过之后列表**离网格**（实测漂移 +11…+82 像素），
而标题带只有 18 像素高装不下那点漂移。**整段（连三条不许调的边界）在
`optional.report_screens._mail_time_bands` 的 docstring 里。**

## 这组数是怎么出来的，以及别怎么填

`CALIBRATION` 那一组是 `tools.mail_unread_probe.propose_calibration` 在桶边界上
挑出来的：落差最大的那一档，两侧各让 1/3 落差留出空档。

⚠️ **不许填一组「看着差不多」的数**。这条禁令不是洁癖：这道闸的失效形态是
**把已读判成未读**，而未读是「必开」的——一组猜出来的阈值会让每趟信箱把开封预算
翻倍烧掉（`tools.pirate_loop.MAIL_UNREAD_MAX_OPENS` 只封住上界，封不住浪费）。
同一个坑仓库里踩过两次，两次都是「这套配方只会这样错」的结论只在手头那几张图上
成立（见 `tools.nav_value_corpus` 的模块头）。所以换了游戏版面之后要重采重标，
办法（三步：采 / 看 / 算）在 `tools.mail_unread_probe`，而**体检指标是日志里的
`unread_unknown`**：标定之后它必须掉下来，居高不下就是这组数在实机上不成立。

## 判据形状：一个量、一个空档

`MailRowColor` 只记**一件事**：这条标题带里「暖色」像素的分布直方图（暖 = R−B）。
黄/橙字的 R−B 很大，白字与偏蓝的面板底色 R−B ≈ 0 或为负，所以两类在这个量上
分得很开（实测 40×）。

- **测量这一步不带任何阈值。** 直方图是对**带内全部像素**统计的，不先挑「墨迹」
  ——挑墨迹要一个亮度门槛，而那个门槛同样没标定过，用它就等于把标定的自由度
  提前焊死（`pirate_loop._value_box_evidence` 里「裁片不做任何预处理」是同一条）。
  自对齐**定位**那一步用的灰度门槛不算：它决定的是「量哪一块」，不是「暖不暖」。
- **判据要一个空档，不是一条线。** 落在 `read_max_share` 与 `unread_min_share`
  之间时交回 `None`（判不出），而不是往某一侧倒。`OUTCOME_INK_THRESHOLD` 的注释
  写着同一条：门槛该落在一个数量级的空档里，不是调出来的参数。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 暖色直方图的分桶宽度与桶数：`clamp(R − B, 0, 255)` 均分成 16 档。
#:
#: 分桶而不是原样存 256 格，是因为这份数据要进 `system_log` 的 payload，
#: 一趟信箱六行、一晚上几十趟。16 个整数对标定够用（判据只问「≥ 某档的占比」），
#: 而 256 格 × 6 行 × 每趟会把日志表写成语料库。
WARMTH_BUCKET_WIDTH = 16
WARMTH_BUCKETS = 16


@dataclass(frozen=True, slots=True)
class MailRowColor:
    """列表页一行标题带的颜色读数。**只描述，不裁决**——裁决在 `classify_unread`。

    ⚠️ **`mean_luminance` 是纯诊断字段，判据不许看它。** 它原先是「第二个候选
    信号」，依据是「未读行整行底色略亮」——**那条观察是错的，2026-09-08 实测撤回**：
    底色是整块面板的**渐变**，已读第 3 行亮度 88 比未读第 1 行（45）和第 2 行
    （59）都亮。留着这一格只为让「颜色量出来了没有」在日志里有个旁证，
    以及万一哪天版面换了、能从存量日志里看出亮度整体漂了多少。
    """

    #: 这条标题带统计了多少像素（= 带面积）。0 表示这一行根本没读到。
    pixels: int
    #: `clamp(R − B, 0, 255)` 的直方图，`WARMTH_BUCKETS` 档，从低到高。
    warmth_buckets: tuple[int, ...]
    #: 标题带的平均亮度。**只进日志与探针，判据不看它**，见类的说明。
    mean_luminance: int

    def warm_share(self, min_warmth: int) -> float:
        """暖到 `min_warmth` 以上的像素占这条标题带的比例。读不到时是 0.0。

        按**桶**算而不是按精确值算：直方图已经把精度损失掉了，这里再假装有
        像素级精度只会让标定出来的数字对不上复算的结果。取「包含
        `min_warmth` 的那一桶及以上」，也就是**往「暖」的一侧倒**——与
        `MailRow.may_be` 刻意往「开」的一侧倒同一个方向。
        """
        if self.pixels <= 0:
            return 0.0
        first = min_warmth // WARMTH_BUCKET_WIDTH
        if first >= len(self.warmth_buckets):
            return 0.0
        return sum(self.warmth_buckets[first:]) / self.pixels


@dataclass(frozen=True, slots=True)
class MailUnreadCalibration:
    """把颜色读数分成未读 / 已读所需的全部标定量。

    ⚠️ **构造时就要求两档之间有空档**（`unread_min_share > read_max_share`）。
    写成同一个数（一条线）的那一版没有「判不出」这个结局，于是任何一次读偏都会
    被**当成一个确定的答案**用出去——而这道闸的两侧代价完全不对称：判成未读会
    多烧开封预算（几十秒），判成已读会让那一封继续和别人抢那 8 封预算、
    一直排到**掉出扫描下限**为止（整段在 `tools.pirate_loop.MAIL_UNREAD_MAX_OPENS`）。
    """

    #: 算「暖」从哪一档起。
    min_warmth: int
    #: 暖色占比到这个数以上才算未读。
    unread_min_share: float
    #: 暖色占比到这个数以下才算已读。
    read_max_share: float
    #: 这组数是在什么上量的（日期 + 语料）。留着是为了让「换了游戏版面之后
    #: 这组数还作不作数」有个可查的出处。
    measured_on: str

    def __post_init__(self) -> None:
        if not 0 <= self.read_max_share < self.unread_min_share <= 1:
            raise ValueError(
                "未读 / 已读两档之间必须留出空档："
                f"read_max_share={self.read_max_share} 必须小于 "
                f"unread_min_share={self.unread_min_share}，且两者都在 0..1 之间"
            )
        if not 0 <= self.min_warmth <= 255:
            raise ValueError(f"min_warmth={self.min_warmth} 不在 0..255 之间")


#: 标定探针那条日志的正文。**取图那侧与捞数那侧必须用同一个字面量**，所以放在
#: 两边都依赖的这里（`tools.pirate_loop._record_mail_unread_probe` 写，
#: `tools.mail_unread_probe` 按它 SELECT）。抄两份的下场是捞出来 0 条而不报错。
MAIL_UNREAD_PROBE_MESSAGE = "信箱列表页未读色标定探针"


#: 生效中的标定。**`None` = 还没标定过，整条链路退回改动之前的行为。**
#:
#: 这一组量于 **2026-09-08 的四屏信箱列表页实拍**（未读 4 行 / 已读 20 行，
#: 覆盖「对齐网格」一屏与「离网格」三屏，漂移 +11 / +23 / +38 / +82 像素）。
#: 标注由**两条独立证据**定（逐像素目视 + 整屏黄墨迹扫描），不是从占比反推的。
#:
#: 门槛**落在空档里，不在钢丝上**：两侧实测 0.2137（未读最小）与 0.0054
#: （已读最大），而两档取在 0.1443 / 0.0748 —— 空档比两侧的读数差还宽。
#:
#: ⚠️ **这两个数比自对齐取样的复算值略往外让了一点，是刻意的。** 现在的取样把
#: 「列表区之外那半行」整条丢掉了（见 `_mail_time_bands`），于是已读那一侧的
#: 实测上界从 0.0054 掉到 0.0000，照它复算会得到 0.1425 / 0.0712。**没有采用**：
#: 那个 0.0054 是页签上的橙色角标漏进标题带量出来的，而「哪天某个版面又让一块
#: 暖色角标漏进某一行」正是这道闸会遇到的错法。用样本更全的那一组，
#: 已读那一侧就多一层余量，而代价只是几行「判不出」（= 退回改动之前的行为）。
CALIBRATION: MailUnreadCalibration | None = MailUnreadCalibration(
    min_warmth=16,
    unread_min_share=0.1443,
    read_max_share=0.0748,
    measured_on="2026-09-08 实拍 4 屏（自对齐标题带）",
)


def classify_unread(
    color: MailRowColor | None,
    calibration: MailUnreadCalibration | None = None,
) -> bool | None:
    """这一行是未读（True）、已读（False），还是**判不出**（None）。

    四种情形交回 `None`，它们在下游是同一件事——**策略层完全退回现有行为**：

    1. 没有标定（`calibration` 与 `CALIBRATION` 都是 `None`）；
    2. 颜色没读到（`color is None`）—— 取图侧没有这个能力、裁剪出错，或者
       **这一行的时刻带定位不到**（自对齐失败，见 `_mail_time_bands`）；
    3. 这一行一个像素都没统计到（`pixels <= 0`，ROI 落在画面外）；
    4. 暖色占比落在两档之间的空档里。

    第 4 种和前三种分开记在日志里（`MailScan.unread_unknown` 只数「读不出」，
    落在空档里的那几行会带着占比进日志），因为它们的处置不同：前三种是
    「这台机器上这条闸没在工作」，第 4 种是「这一行确实像谁都不像」。
    """
    resolved = calibration if calibration is not None else CALIBRATION
    if resolved is None or color is None or color.pixels <= 0:
        return None
    share = color.warm_share(resolved.min_warmth)
    if share >= resolved.unread_min_share:
        return True
    if share <= resolved.read_max_share:
        return False
    return None
