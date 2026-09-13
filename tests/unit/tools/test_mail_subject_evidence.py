"""主题读不出的那些行，现场要留下**够判下一步的**三样东西。

## 为什么要有这一条

2026-09-12 那一夜 65 分钟：开封 58 封 → 入库 7 封、白开 50 封，约 8 分钟白花。
58 封里 55 封的主题读数连「攻击报告」「海盗」字样都没有，读成
`'TTT     seesere'` / `'一一 band a | rt rm kar Ae'` / `'~~ ae te'`。

而日志里**只有**这串噪声。噪声本身答不了下一步该做什么：

| 可能 | 该做的 |
|---|---|
| ROI 把字切了 | 换框（纵向自对齐） |
| 那几个像素本来就糊 | 换配方（放大 / 二值化） |

两者处置相反，而分开它们需要**像素**和**一次对照读**。所以这条记录里三样缺一
不可：离网格量（机器读）、同一帧自对齐重读的结果（机器读）、原分辨率裁片
（人读）。少任何一样，事后都还是只能猜。

## ⚠️ 它一个判断都不改

主题读不出仍旧归 `UNKNOWN`、仍旧照开。「读不出绝不能往不开那一侧倒」
（`MailRow.unread` 上方那条）在这次改动里一个字都没动——把主题读不出倒向
「不开」，代价是一封主题被糊掉的**真战报永远不会被开**，比白开严重得多。
本文件末尾专门有一条钉这件事。
"""

from __future__ import annotations

from typing import Any

import pytest

from evo_helper.tools import pirate_loop as module
from evo_helper.tools.pirate_loop import MailRow
from evo_helper.vision.parsers import ReportKind


class _Recorder:
    """替掉 `record_system_log`，把 payload 收下来。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, level: str, _source: str, message: str, *, payload: Any) -> None:
        self.calls.append((level, message, payload))

    @property
    def subject_payloads(self) -> list[dict[str, Any]]:
        return [
            payload
            for _level, message, payload in self.calls
            if message == module.MAIL_SUBJECT_EVIDENCE_MESSAGE
        ]


class _Screens:
    """只实现这条取证要用到的那几样。`aligned` 给哪几行、`crop_raises` 让裁片抛。"""

    def __init__(
        self,
        offsets: tuple[int | None, ...],
        *,
        aligned: dict[int, str] | None = None,
        crop_raises: bool = False,
    ) -> None:
        self._offsets = offsets
        self._aligned = aligned or {}
        self._crop_raises = crop_raises
        self.aligned_calls: list[int] = []

    def mail_title_band_offsets(self) -> tuple[int | None, ...]:
        return self._offsets

    def mail_row_aligned(self, index: int) -> str | None:
        self.aligned_calls.append(index)
        return self._aligned.get(index)

    def mail_row_crops(self) -> tuple[Any, ...]:
        if self._crop_raises:
            raise RuntimeError("Pillow 不高兴")
        from PIL import Image

        return tuple(
            Image.new("RGB", (536, 101), (index * 20, 0, 0)) for index in range(len(self._offsets))
        )


class _TextOnlyScreens:
    """只答得出离网格量的那种：没有裁片、也没有自对齐重读。"""

    def mail_title_band_offsets(self) -> tuple[int | None, ...]:
        return (24,) * 6


def _row(index: int, subject: str, *, kind: ReportKind, unread: bool | None = None) -> MailRow:
    return MailRow(
        index=index,
        subject=subject,
        raw_time_text="12/09/2026 21:30:00",
        reported_at_utc=None,
        kind=kind,
        unread=unread,
    )


def _unreadable(index: int, *, unread: bool | None = None) -> MailRow:
    return _row(index, "一一 band a | rt rm kar Ae", kind=ReportKind.UNKNOWN, unread=unread)


@pytest.fixture
def looper(monkeypatch: pytest.MonkeyPatch) -> Any:
    recorder = _Recorder()
    monkeypatch.setattr(module, "record_system_log", recorder)
    # ⚠️ 跨轮那道限流的状态挂在**模块**上（进程级）。不清干净的话，用例之间会
    # 互相掐掉对方的图，而那种红是按文件顺序变的。
    module._last_evidence_frame_at.clear()
    looper = module.PirateLoop.__new__(module.PirateLoop)
    return looper, recorder


# -- 记不记 ----------------------------------------------------------------------


def test_a_screen_whose_subjects_all_classify_records_nothing(looper: Any) -> None:
    """主题都认出来了就一个字不记。这条取证是**故障现场**，不是逐屏流水账。"""
    obj, recorder = looper

    obj._record_unreadable_subject_evidence(
        _Screens((48,) * 6),
        [_row(index, "攻击报告", kind=ReportKind.ATTACK) for index in range(6)],
    )

    assert recorder.subject_payloads == []


def test_the_offset_rides_along_with_every_unreadable_row(looper: Any) -> None:
    """⚠️⚠️ **离网格量是这条记录的主证据**，而且**每一行**都要有。

    偏移 < 30 就是「主题被 ROI 上沿横着切掉」（整段在
    `vision.optional.report_screens.mail_title_band_offsets`）。没有这个数，
    日志里剩下的只是一串噪声字符串，而噪声既可能是框错了、也可能是像素糊了。
    """
    obj, recorder = looper
    rows = [_unreadable(0), _row(1, "攻击报告", kind=ReportKind.ATTACK), _unreadable(2)]

    obj._record_unreadable_subject_evidence(_Screens((24, 24, 25, None, None, None)), rows)

    payload = recorder.subject_payloads[0]
    assert payload["unreadable"] == 2
    assert payload["rows_on_screen"] == 3
    assert [entry["index"] for entry in payload["rows"]] == [0, 2]
    assert [entry["title_band_offset"] for entry in payload["rows"]] == [24, 25]


def test_it_is_recorded_as_a_warning(looper: Any) -> None:
    """按 WARNING 记：这一行**花掉了一次开封预算**，不是一条 INFO 流水。"""
    obj, recorder = looper

    obj._record_unreadable_subject_evidence(_Screens((24,) * 6), [_unreadable(0)])

    assert recorder.calls[0][0] == "WARNING"


def test_a_row_without_an_anchor_says_so_instead_of_pretending(looper: Any) -> None:
    """⚠️ 定位不到的行照样进记录，偏移留 `None`。

    「这一行的框错了」和「这一行连锚点都没找到」在账上必须分得开——后者说明
    自对齐这条路对它本来就无效，而那是换框方案的已知代价。
    """
    obj, recorder = looper

    obj._record_unreadable_subject_evidence(_Screens((None,) * 6), [_unreadable(0)])

    assert recorder.subject_payloads[0]["rows"][0]["title_band_offset"] is None


# -- 贵的那两样：裁片与自对齐重读 ---------------------------------------------------


def test_the_aligned_re_read_lands_with_its_verdict(looper: Any) -> None:
    """⚠️⚠️ **同一帧、同一套配方、只换框**，读出来是什么、会被判成什么，一起记。

    这一条就是整份取证的判据：读得出 ⇒ 那几个像素是好的、框错了 ⇒ 该换框；
    读不出 ⇒ 换框救不了这一行 ⇒ 才轮到配方那一侧。

    判定走生产那一条 `mail_row_from_text`，不在取证里另写一份——另写一份就会和
    生产分岔，而这条记录存在的意义正是拿它去预判换框值不值得。
    """
    obj, recorder = looper
    screens = _Screens((24,) * 6, aligned={0: "攻击报告\nSystem\n12/09/2026 21:30:00"})

    obj._record_unreadable_subject_evidence(screens, [_unreadable(0)])

    evidence = recorder.subject_payloads[0]["evidence_rows"][0]
    assert evidence["index"] == 0
    assert evidence["aligned_kind"] == ReportKind.ATTACK.name
    assert "攻击报告" in evidence["aligned_subject"]
    assert evidence["aligned_time_text"] == "12/09/2026 21:30:00"


def test_the_crop_is_stored_at_full_resolution(looper: Any) -> None:
    """⚠️ 原分辨率。480 宽的整帧缩略图上这行小字是一团糊斑，存了也读不出字。

    而「那四个字到底是什么」只有人眼答得了——机器读的那两样都只说得出
    「读不出」，说不出「本该是舰队返回」。
    """
    import base64
    import io

    from PIL import Image

    obj, recorder = looper

    obj._record_unreadable_subject_evidence(_Screens((24,) * 6), [_unreadable(0)])

    encoded = recorder.subject_payloads[0]["evidence_rows"][0]["mail_row_png_base64"]
    assert Image.open(io.BytesIO(base64.b64decode(encoded))).size == (536, 101)


def test_only_a_couple_of_rows_pay_for_pixels_and_a_re_read(looper: Any) -> None:
    """⚠️⚠️ **文字每一行都记，图和重读只给两行。**

    两样都贵而且贵在不同地方：一行裁片 base64 之后约 69 KB，而自对齐重读是一次
    真的 OCR（几百毫秒，直接加在开封循环上）。一屏六行全带上就是 400 KB + 两三秒。
    """
    obj, recorder = looper
    screens = _Screens((24,) * 6)

    obj._record_unreadable_subject_evidence(screens, [_unreadable(index) for index in range(6)])

    payload = recorder.subject_payloads[0]
    assert len(payload["rows"]) == 6
    assert len(payload["evidence_rows"]) == module.PirateLoop.MAIL_SUBJECT_EVIDENCE_CROPS == 2
    assert len(screens.aligned_calls) == 2


def test_the_budget_goes_to_the_unread_rows_first(looper: Any) -> None:
    """⚠️ 名额先给未读行：它们是「必开」那一档，白开的成本由它们付。

    读不出（`None`）排在已读之前，同 `MailRow.unread`——读不出按「可能是未读」办。
    """
    obj, recorder = looper
    rows = [
        _unreadable(0, unread=False),
        _unreadable(1, unread=None),
        _unreadable(2, unread=True),
    ]

    obj._record_unreadable_subject_evidence(_Screens((24,) * 6), rows)

    picked = [entry["index"] for entry in recorder.subject_payloads[0]["evidence_rows"]]
    assert picked == [2, 1]


def test_the_second_screen_inside_the_window_keeps_its_text_but_loses_its_pixels(
    looper: Any,
) -> None:
    """⚠️ 跨轮限流**只掐图，不掐文字**（同 `_allow_evidence_frame` 那条）。

    判据把活儿挡掉的次数必须在库里数得清，而那是文字回答的；图回答的是
    「当时看到了什么」，少一张不影响计数。被掐掉时 payload 上要留痕——
    不留痕的话，这条记录和「当时截不到图」长得一模一样。
    """
    obj, recorder = looper

    obj._record_unreadable_subject_evidence(_Screens((24,) * 6), [_unreadable(0)])
    obj._record_unreadable_subject_evidence(_Screens((25,) * 6), [_unreadable(1)])

    second = recorder.subject_payloads[1]
    assert second["rows"][0]["title_band_offset"] == 25
    assert "evidence_rows" not in second
    assert module.EVIDENCE_THROTTLED_KEY in second


def test_the_process_budget_stops_the_text_too(looper: Any) -> None:
    """文字那一半也有名额，只是宽得多。**信箱一趟翻八屏，这不是一条流水线。**"""
    obj, recorder = looper
    budget = module.PirateLoop.MAX_MAIL_SUBJECT_EVIDENCE

    for _ in range(budget + 3):
        obj._record_unreadable_subject_evidence(_Screens((24,) * 6), [_unreadable(0)])

    assert len(recorder.subject_payloads) == budget


# -- 取证不许弄死链路 --------------------------------------------------------------


def test_a_screens_without_the_new_methods_records_nothing_and_does_not_raise(
    looper: Any,
) -> None:
    """轻量测试桩、`tools.ingest_report` 那个只实现文字的 `ReportScreens`——
    一律安静地不记。

    ⚠️ **不往 `ReportScreens` 协议上加方法**（同 `_mail_row_unread` 那条）：
    加了就得改每一个实现，而它们一个都不需要这个信号。
    """
    obj, recorder = looper

    obj._record_unreadable_subject_evidence(object(), [_unreadable(0)])

    assert recorder.subject_payloads == []


def test_a_screens_without_pixels_still_records_the_offsets(looper: Any) -> None:
    """能给离网格量、给不出裁片与重读时，**主证据照记**。

    文字那一半本身就够回答「这一屏离网格多少」，而那是这条取证的核心问题。
    """
    obj, recorder = looper

    obj._record_unreadable_subject_evidence(_TextOnlyScreens(), [_unreadable(0)])

    payload = recorder.subject_payloads[0]
    assert payload["rows"][0]["title_band_offset"] == 24
    assert payload["evidence_rows"] == [{"index": 0}]


def test_a_crop_that_blows_up_does_not_take_the_re_read_with_it(looper: Any) -> None:
    """⚠️ 一样取不到只丢掉这一样，文字那一半在任何情况下都已经落库了。

    取证抛异常弄死一趟信箱是本末倒置：这一趟能救回来的战报比一张图值钱。
    """
    obj, recorder = looper
    screens = _Screens((24,) * 6, crop_raises=True, aligned={0: "攻击报告"})

    obj._record_unreadable_subject_evidence(screens, [_unreadable(0)])

    evidence = recorder.subject_payloads[0]["evidence_rows"][0]
    assert evidence["crop_failed"] is True
    assert evidence["aligned_kind"] == ReportKind.ATTACK.name
    assert recorder.subject_payloads[0]["rows"][0]["title_band_offset"] == 24


def test_offsets_that_blow_up_leave_the_loop_alone(looper: Any) -> None:
    """连主证据都取不到时整条不记 —— 但**不抛**。"""

    class _Broken:
        def mail_title_band_offsets(self) -> tuple[int | None, ...]:
            raise RuntimeError("ROI 出画了")

    obj, recorder = looper

    obj._record_unreadable_subject_evidence(_Broken(), [_unreadable(0)])

    assert recorder.subject_payloads == []


# -- 判据没被动 ------------------------------------------------------------------


def test_an_unreadable_subject_is_still_opened(looper: Any) -> None:
    """⚠️⚠️ **这次改的是取证，不是闸门。**

    主题读不出仍旧是 `UNKNOWN`、仍旧过得了主题闸。把它倒向「不开」，代价是一封
    主题被糊掉的**真战报永远不会被开**——比白开八秒严重得多。
    """
    row = _unreadable(0)

    assert row.kind is ReportKind.UNKNOWN
    assert row.may_be(ReportKind.ATTACK) is True
    assert row.may_be([ReportKind.PIRATE, ReportKind.ATTACK]) is True
