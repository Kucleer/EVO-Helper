"""用 49 张**失败现场**守住这次修法：飞行时间读不出来的那些，到达时间读得出来。

事故：实机 2026-08-18，24 小时 62 发派遣里 14 发读不到飞行时间（23%），最近
6 小时 3/10。读不到的代价不是白跑一趟，是那一发按
`domain.report_wait.UNKNOWN_LINE_HOLD`（90 分钟）占着航线，而实测往返只有约
46 分钟——**每次白占约 44 分钟，一天约 10 航线小时**。

成因是**中文字符**：`分` 被读成 `5)` / `5}`，`秒` 被读成 `%` / `%)`
（生产 `system_log` 原始读数：`'245} 15秒'`、`'225) 48秒'`、`'265) 41%'`）。
数字一位不差。

修法是**换来源**而不是加配方：`var/logs/dump-briefing-flight-unreadable-*.png`
上跑了 5 档放大 × 11 档阈值的整张网格，零读错的配方全部取并集也只覆盖 23/47，
而「预计到达时间」那两行（`16/08/2026` + `09:31:27`，纯数字加分隔符）
在同一批图上 47/47。

截图在 `var/logs` 下，**不进 Git**（公开仓库），所以缺图时整个文件跳过。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from evo_helper.domain.flight_estimate import (
    CALIBRATED_FLEET_SPEED,
    CALIBRATED_SPEED_PERCENT,
)
from evo_helper.domain.report_wait import parse_game_duration
from evo_helper.game.pirate_ui import (
    ARRIVAL_RECIPES,
    BRIEFING_ARRIVAL_DATE_ROI,
    BRIEFING_ARRIVAL_TIME_ROI,
    BRIEFING_FLIGHT_ROI,
    BRIEFING_SPEED_PERCENT_ROI,
    BRIEFING_SPEED_ROI,
    FLIGHT_RECIPES,
)
from evo_helper.vision.parsers import GAME_DISPLAY_ZONE, parse_report_timestamp

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

DUMPS = sorted(Path("var/logs").glob("dump-briefing-flight-unreadable-*.png"))

#: 存图那一刻的本机时区（文件名里的 `HHMMSS` 就是它）。
LOCAL = timezone(timedelta(hours=8))

#: 人工核过的真值：`存图时刻 -> (飞行时长, 画面上的到达时刻 UTC)`。
#:
#: 核定办法是**两条互相独立的证据**，都对上才写进来：
#:
#: 1. 目视放大后的裁片；
#: 2. `到达时刻 - 存图时刻` 应当等于画面上的飞行时长——47 张全部对上，误差 ≤1 秒。
#:
#: 第 2 条顺带量出了本次修法赖以成立的那件事（见
#: `test_the_arrival_line_is_recomputed_every_second`）。
#:
#: 两张面板压根没铺开的（`030027` / `140253`）故意留成 None：那两张**本来就
#: 该读不出来**，是这批样本里唯一的阴性对照。
TRUTH: dict[str, tuple[tuple[int, int] | None, str | None]] = {
    "004736": ((15, 20), "13/08/2026 17:02:56"),
    "030027": (None, None),
    "040101": ((22, 15), "15/08/2026 20:23:15"),
    "040201": ((24, 23), "15/08/2026 20:26:24"),
    "040834": ((27, 36), "15/08/2026 20:36:09"),
    "043809": ((27, 36), "15/08/2026 21:05:44"),
    "044031": ((28, 22), "15/08/2026 21:08:53"),
    "044500": ((28, 22), "15/08/2026 21:13:21"),
    "050037": ((22, 57), "13/08/2026 21:23:34"),
    "050238": ((22, 40), "13/08/2026 21:25:18"),
    "060936": ((24, 23), "15/08/2026 22:33:59"),
    "061053": ((21, 16), "13/08/2026 22:32:09"),
    "061312": ((38, 28), "15/08/2026 22:51:39"),
    "061333": ((20, 49), "13/08/2026 22:34:22"),
    "061718": ((17, 46), "15/08/2026 22:35:04"),
    "065247": ((17, 46), "15/08/2026 23:10:33"),
    "065417": ((23, 29), "13/08/2026 23:17:45"),
    "070132": ((21, 50), "13/08/2026 23:23:22"),
    "070413": ((40, 40), "15/08/2026 23:44:52"),
    "072725": ((41, 38), "16/08/2026 00:09:02"),
    "125700": ((40, 26), "16/08/2026 05:37:26"),
    "125801": ((41, 38), "16/08/2026 05:39:38"),
    "125902": ((41, 43), "16/08/2026 05:40:44"),
    "140253": (None, None),
    "140920": ((31, 23), "16/08/2026 06:40:43"),
    "141401": ((31, 23), "16/08/2026 06:45:24"),
    "141932": ((31, 23), "16/08/2026 06:50:54"),
    "155555": ((30, 29), "16/08/2026 08:26:23"),
    "160022": ((30, 29), "16/08/2026 08:30:50"),
    "160553": ((30, 29), "16/08/2026 08:36:21"),
    "161055": ((30, 29), "16/08/2026 08:41:24"),
    "161525": ((30, 29), "16/08/2026 08:45:53"),
    "162058": ((30, 29), "16/08/2026 08:51:27"),
    "162524": ((30, 29), "16/08/2026 08:55:52"),
    "163025": ((30, 29), "16/08/2026 09:00:53"),
    "163527": ((30, 29), "16/08/2026 09:05:56"),
    "164025": ((30, 29), "16/08/2026 09:10:54"),
    "164527": ((30, 29), "16/08/2026 09:15:56"),
    "165059": ((30, 29), "16/08/2026 09:21:27"),
    "165559": ((30, 29), "16/08/2026 09:26:27"),
    "170059": ((30, 29), "16/08/2026 09:31:27"),
    "170601": ((30, 29), "16/08/2026 09:36:30"),
    "171101": ((30, 29), "16/08/2026 09:41:30"),
    "171602": ((30, 29), "16/08/2026 09:46:30"),
    "172134": ((22, 40), "16/08/2026 09:44:14"),
    "172234": ((36, 39), "16/08/2026 09:59:13"),
    "172645": ((36, 39), "16/08/2026 10:03:23"),
    "173144": ((36, 39), "16/08/2026 10:08:23"),
    "235608": ((15, 20), "13/08/2026 16:11:27"),
}

pytestmark = pytest.mark.skipif(
    not DUMPS or {path.stem[-6:] for path in DUMPS} - set(TRUTH),
    reason="缺实拍现场 var/logs/dump-briefing-flight-unreadable-*.png（截图不进 Git）",
)

READABLE = [stamp for stamp, (flight, _) in TRUTH.items() if flight is not None]


@pytest.fixture(scope="module")
def ocr():  # type: ignore[no-untyped-def]
    from evo_helper.tools.scan_coordinates import make_ocr

    return make_ocr()


def _frame(stamp: str):  # type: ignore[no-untyped-def]
    return Image.open(f"var/logs/dump-briefing-flight-unreadable-{stamp}.png")


def _truth_arrival(stamp: str) -> datetime:
    text = TRUTH[stamp][1]
    assert text is not None
    return datetime.strptime(text, "%d/%m/%Y %H:%M:%S").replace(tzinfo=UTC)


def _read_arrival(ocr, stamp: str, recipes) -> datetime | None:  # type: ignore[no-untyped-def]
    """照 `PirateLoop._read_arrival_flight()` 那条路读：第一个拼得成时间戳的算数。"""
    frame = _frame(stamp)
    for upscale, threshold in recipes:
        date_text = ocr(
            frame.crop(BRIEFING_ARRIVAL_DATE_ROI),
            digits=False,
            upscale=upscale,
            threshold=threshold,
        )
        time_text = ocr(
            frame.crop(BRIEFING_ARRIVAL_TIME_ROI),
            digits=False,
            upscale=upscale,
            threshold=threshold,
        )
        arrival = parse_report_timestamp(f"{date_text} {time_text}", GAME_DISPLAY_ZONE)
        if arrival is not None:
            return arrival
    return None


# -- 主来源：到达时间 --------------------------------------------------------


@pytest.mark.parametrize("stamp", READABLE)
def test_the_arrival_time_is_readable_on_every_frame_the_flight_line_failed(  # type: ignore[no-untyped-def]
    ocr, stamp: str
) -> None:
    """⚠️ **本次修法的落点。** 这 47 张全是飞行时间那一行读不出来的现场。

    读不出来的代价不是白跑一趟，是那一发按 `UNKNOWN_LINE_HOLD`（90 分钟）
    占着航线，而实测往返只有约 46 分钟。
    """
    assert _read_arrival(ocr, stamp, ARRIVAL_RECIPES) == _truth_arrival(stamp)


def test_the_flight_line_really_could_not_be_read_on_these_frames(ocr) -> None:  # type: ignore[no-untyped-def]
    """把「为什么非换来源不可」钉住：这批图上飞行时间那一行确实读不出多少。

    没有这条，上面那 47 条会被人误读成「顺手加的一个备份」，而它其实是主路径。
    量出来的数字写在这里，日后 tesseract 换版本、这条变松了，该做的是回来
    重写 `pirate_ui.FLIGHT_RECIPES` 上那段复标，而不是把这条删掉。
    """
    read = 0
    for stamp in READABLE:
        crop = _frame(stamp).crop(BRIEFING_FLIGHT_ROI)
        for upscale, threshold in FLIGHT_RECIPES:
            if (
                parse_game_duration(ocr(crop, digits=False, upscale=upscale, threshold=threshold))
                is not None
            ):
                read += 1
                break
    # 补了六套零读错的配方之后是 23/47；换来源那一路是 47/47。
    assert read < len(READABLE) * 0.6, f"飞行时间那一行读出了 {read}/{len(READABLE)}"


def test_no_arrival_recipe_ever_produces_a_wrong_timestamp(ocr) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **这条比「能读出来」更要紧。**

    这条路取的是**第一个拼得成时间戳的**，所以配方表里只要有一套会「成功地
    读错」，排在前面就会把错值写进库——而错值同时污染两个钟，还一声不响。

    实测被这条挡在外面的：`3×/None` 读对 45/47，却把 `09:26:27` 读成
    `03:26:27`（差六小时）；`2×/140` 把 `21:13:21` 读成 `20:13:21`。
    **读对得更多也不许进来。**
    """
    wrong: list[str] = []
    for stamp in READABLE:
        frame = _frame(stamp)
        for upscale, threshold in ARRIVAL_RECIPES:
            date_text = ocr(
                frame.crop(BRIEFING_ARRIVAL_DATE_ROI),
                digits=False,
                upscale=upscale,
                threshold=threshold,
            )
            time_text = ocr(
                frame.crop(BRIEFING_ARRIVAL_TIME_ROI),
                digits=False,
                upscale=upscale,
                threshold=threshold,
            )
            got = parse_report_timestamp(f"{date_text} {time_text}", GAME_DISPLAY_ZONE)
            if got is not None and got != _truth_arrival(stamp):
                wrong.append(f"{stamp} {upscale}x/thr{threshold} 读成 {got}")
    assert wrong == []


def test_reading_the_two_lines_as_one_roi_reads_nothing_at_all(ocr) -> None:  # type: ignore[no-untyped-def]
    """**为什么必须拆成两个 ROI**——把这件事本身钉住。

    实机版面把到达时间排成两行（日期一行、时分秒另一行），而本仓取字一律
    `--psm 7`（单行）。合成一个两行的框去读，49 张上读出来全是空串。
    没有这条，有人日后「顺手合并」这两个 ROI 会一路绿灯回到 23% 那个状态。
    """
    whole = (
        BRIEFING_ARRIVAL_DATE_ROI[0],
        BRIEFING_ARRIVAL_DATE_ROI[1],
        BRIEFING_ARRIVAL_TIME_ROI[2],
        BRIEFING_ARRIVAL_TIME_ROI[3],
    )
    texts = {
        ocr(_frame(stamp).crop(whole), digits=False, upscale=2, threshold=None).strip()
        for stamp in READABLE[:8]
    }

    assert texts == {""}


# -- 减法凭什么成立 ----------------------------------------------------------


@pytest.mark.parametrize("stamp", READABLE)
def test_the_arrival_line_is_recomputed_every_second(stamp: str) -> None:
    """`到达时间 - 现在` 就是剩余飞行时长——**这是量出来的，不是推的**。

    文件名里的 `HHMMSS` 是 `_dump_frame` 存图那一刻的本机时刻。若到达时间是
    面板铺开那一刻定死的，这个差应当稳定地是负的（存图比铺开晚好几秒）；
    实测 47 张全部落在 {-1 秒, 0 秒}，也就是说它**每秒重算**，而且本机时钟
    与游戏时钟是同步的。

    这条同时是 `_read_arrival_flight` 里那个减法的全部依据。它哪天红了
    （游戏改成静态显示、或者本机时钟漂了），那个减法就不能再用。
    """
    flight, _ = TRUTH[stamp]
    assert flight is not None
    hours, minutes, seconds = int(stamp[:2]), int(stamp[2:4]), int(stamp[4:6])
    implied = (_truth_arrival(stamp) - timedelta(minutes=flight[0], seconds=flight[1])).astimezone(
        LOCAL
    )
    drift = (implied.hour * 3600 + implied.minute * 60 + implied.second) - (
        hours * 3600 + minutes * 60 + seconds
    )

    assert -1 <= drift <= 0, f"{stamp} 差了 {drift} 秒"


# -- 第三个来源的适用域闸 ----------------------------------------------------


@pytest.mark.parametrize("stamp", READABLE)
def test_the_fleet_speed_is_readable_with_the_default_recipe(ocr, stamp: str) -> None:  # type: ignore[no-untyped-def]
    """速度那两格用**默认配方**就读得出来，49 张里 47 张（另 2 张面板没铺开）。

    它存在的唯一理由是给 `domain.flight_estimate.predict_flight` 判一句
    「这一发还在 `domain.flight_time` 标定过的那套编组上吗」。读不出来时那条
    判据返回 False、公式弃权——**弃权是安全的那一侧**，只是少一个来源。

    白字压蓝底，和「没有可执行的任务」那个弹窗一样好读，与绿字压蓝底的
    飞行时间正相反。
    """
    frame = _frame(stamp)

    assert ocr(frame.crop(BRIEFING_SPEED_ROI), digits=False, upscale=3).strip() == (
        CALIBRATED_FLEET_SPEED
    )
    assert ocr(frame.crop(BRIEFING_SPEED_PERCENT_ROI), digits=False, upscale=3).strip() == (
        CALIBRATED_SPEED_PERCENT
    )


@pytest.mark.parametrize("stamp", ["030027", "140253"])
def test_a_frame_where_the_panel_never_rendered_reads_nothing(ocr, stamp: str) -> None:  # type: ignore[no-untyped-def]
    """阴性对照：面板没铺开的那两张，三个来源**都该读不出来**。

    没有这条，「一律返回一个值」也能让上面那些变绿。而这两张正是实机日志里
    那两条空读数（`''`）的现场——它们不是这次要修的东西，重试才是。
    """
    assert _read_arrival(ocr, stamp, ARRIVAL_RECIPES) is None
    assert ocr(_frame(stamp).crop(BRIEFING_SPEED_ROI), digits=False, upscale=3).strip() != (
        CALIBRATED_FLEET_SPEED
    )
