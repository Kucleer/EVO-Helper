"""胜负的两条来源，跑在 2026-08-11 那批实拍上。

胜负**以画面横幅为准**（用户口径 2026-08-17：「游戏算法更新，剩余舰艇算法已经
不准了，可以读 victory」），横幅读不出来才回落到「剩余 = 单位 − 损失单位」
（`domain.battle_outcome`）。仲裁规则本身在
`tests/unit/vision/test_pirate_reports.py` 里用假取字面钉着；这个文件管的是
**另一半**——横幅与那四个数在真画面上到底读不读得出来、在哪一屏上读得出来。

这一条不能用假 OCR 代替。假取字面只能证明接线对；而真正决定成败的是
「`损失单位` 这一行在没拖过的那屏上一个字都读不到」这件事实——它是
「必须拖到底」这个结论的全部依据，只有真图能给。

样本（整窗 1920×917，`var/` 是运行期目录，不进 Git，所以缺样本就跳过）：

- `dump-probe-report-unreadable-1043*.png`：五份 bot 探路战报详情页（未滚动）
- `rep-9-detail.png`：另一份 bot 战报详情页（未滚动）
- `pir1-detail.png`：海盗战报详情页（未滚动，另一个 ui_version）
- `pir1-bottom.png`：同一份海盗战报**拖到底**之后那一屏——仓库里唯一一屏
  四个数齐全的实拍

横幅（`VICTORY` / `FAIL` 那行大字）逐张钉着——它现在是第一判据，读错一张就是
库里一条假战果。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from evo_helper.domain.battle_outcome import OUTCOME_FAIL, OUTCOME_VICTORY, outcome_from_totals
from evo_helper.vision.pirate_reports import parse_outcome
from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport

Image = pytest.importorskip("PIL.Image", reason="requires the vision extra")
pytest.importorskip("pytesseract", reason="requires the vision extra")

LOGS = Path("var/logs")
TESSERACT = os.environ.get("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

#: `(样本名, 横幅期望值, 「单位」期望读数)`。
#:
#: 「单位」那一列写 `("", "")` 的**不是读坏了**，是那一行根本不在这一屏上：
#: bot 战报比海盗战报多一行「生成卫星概率」，「战斗详情」横幅下移约 30px，
#: 「单位」整行落到面板可视区之外。`104400` 恰好没有那一行，于是读得出来。
DETAIL_SAMPLES = (
    ("dump-probe-report-unreadable-104251", "FAIL", ("", "")),
    ("dump-probe-report-unreadable-104300", "FAIL", ("", "")),
    ("dump-probe-report-unreadable-104321", "FAIL", ("", "")),
    ("dump-probe-report-unreadable-104352", "FAIL", ("", "")),
    ("dump-probe-report-unreadable-104400", "FAIL", ("1", "319")),
    ("rep-9-detail", "FAIL", ("1", "247")),
    ("pir1-detail", "VICTORY", ("100", "783")),
)

#: 拖到底之后那一屏：横幅滚走了，「单位」与「损失单位」两行都露出来。
BOTTOM_SAMPLE = "pir1-bottom"

_ALL = [name for name, _banner, _units in DETAIL_SAMPLES] + [BOTTOM_SAMPLE]

pytestmark = pytest.mark.skipif(
    not (Path(TESSERACT).is_file() and all((LOGS / f"{name}.png").is_file() for name in _ALL)),
    reason="2026-08-11 战报实拍或 Tesseract 不可用",
)


def screens(name: str):  # type: ignore[no-untyped-def]
    from evo_helper.vision.optional.report_screens import ImageReportScreens

    image = crop_to_viewport(Image.open(LOGS / f"{name}.png"))
    layout = layout_for_viewport(image.width, image.height)
    return ImageReportScreens(image, layout, tesseract_cmd=TESSERACT)  # type: ignore[arg-type]


# -- 判据的四个输入，在哪一屏上读得到 ----------------------------------------


@pytest.mark.parametrize(("name", "_banner", "units"), DETAIL_SAMPLES)
def test_the_unit_row_is_only_visible_on_some_unscrolled_screens(
    name: str, _banner: str, units: tuple[str, str]
) -> None:
    """七张里五张读作 `("", "")`——**不是锚点找歪了**。

    `_details_banner_bottom()` 在那五张上返回 None，因为「战斗详情」横幅下面根本
    没有一行能解析的数；肉眼看图也一样，横幅之下直接就是转发/收藏/删除三个图标。
    """
    assert screens(name).unit_totals() == units


@pytest.mark.parametrize(("name", "_banner", "_units"), DETAIL_SAMPLES)
def test_no_unscrolled_screen_yields_the_losses_at_all(
    name: str, _banner: str, _units: tuple[str, str]
) -> None:
    """⚠️ **这一条是「必须拖到底」的全部依据。**

    七张未滚动的详情页，**没有一张**读得出「损失单位」——bot 那六张那一行压根
    没画出来，海盗那张被面板下沿切掉、只读到半个字符 `'.'`（解析不成数）。

    而战损是算胜负的两个输入之一。所以不拖那一屏，攻击日志的战果列会永远空着，
    这跟 OCR 调得好不好一点关系都没有。
    """
    from evo_helper.domain.fleet_counts import parse_fleet_count

    left, right = screens(name).loss_totals()

    assert parse_fleet_count(left) is None or parse_fleet_count(right) is None


def test_the_scrolled_screen_is_the_only_one_with_all_four_numbers() -> None:
    """拖到底那一屏上「单位」与「损失单位」两行都读得出来——这就是那一步的收益。"""
    bottom = screens(BOTTOM_SAMPLE)

    assert bottom.unit_totals() == ("100", "783")
    assert bottom.loss_totals() == ("0", "783")


def test_the_fallback_arithmetic_off_the_real_numbers() -> None:
    """仓库里唯一一屏四个数齐全的实拍，把**兜底那条路**端到端算一遍。

    我方 100−0 = 100（还有船），对方 783−783 = 0（被全歼）→ `VICTORY`。
    """
    from evo_helper.domain.fleet_counts import parse_fleet_count

    bottom = screens(BOTTOM_SAMPLE)
    units = bottom.unit_totals()
    losses = bottom.loss_totals()

    assert (
        outcome_from_totals(
            attacker_units=parse_fleet_count(units[0]),
            attacker_losses=parse_fleet_count(losses[0]),
            defender_units=parse_fleet_count(units[1]),
            defender_losses=parse_fleet_count(losses[1]),
        )
        == OUTCOME_VICTORY
    )


def test_the_arithmetic_and_the_banner_agree_on_the_one_report_we_can_check_both_ways() -> None:
    """⚠️ 唯一一份两条路都走得通的实拍，**两条路结论一致**。

    横幅取自没拖过那一屏（读作 `'VICTORY'`），算式取自同一份报告拖到底那一屏的
    四个数（→ `VICTORY`）。两个来源相互独立：一个是一行半透明大字的字形，
    一个是两行小字的数值。它们对上，说明把兜底留在算式上是有依据的——
    2026-08-17 之前算式还准的时候，这就是它们对得上的原因。
    """
    from evo_helper.domain.fleet_counts import parse_fleet_count

    bottom = screens(BOTTOM_SAMPLE)
    units, losses = bottom.unit_totals(), bottom.loss_totals()
    computed = outcome_from_totals(
        attacker_units=parse_fleet_count(units[0]),
        attacker_losses=parse_fleet_count(losses[0]),
        defender_units=parse_fleet_count(units[1]),
        defender_losses=parse_fleet_count(losses[1]),
    )

    assert computed == parse_outcome(screens("pir1-detail").outcome_banner())


# -- 横幅：第一判据，必须逐张读得准 ------------------------------------------


@pytest.mark.parametrize(("name", "banner", "_units"), DETAIL_SAMPLES)
def test_the_banner_reads_on_every_captured_detail_screen(
    name: str, banner: str, _units: tuple[str, str]
) -> None:
    """七张全对，两种颜色两个 ui_version。

    2026-08-17 起它是第一判据，这一条因此从「校验的校验」升成了**战果的地基**：
    改坏门槛或去掉通道分离，`FAIL` 那五张立刻退回 `'- a'`，而那五份战报的战果
    会跟着退回「靠一套已知不准的算术去猜」。
    """
    assert parse_outcome(screens(name).outcome_banner()) == banner


def test_the_scrolled_screen_yields_no_banner_at_all() -> None:
    """⚠️ 拖到底之后横幅已经滚出可视区，同一段 ROI 里只剩资源图标与背景。

    实测读作 `'Z ?'`。所以横幅必须取**没拖过那一屏**的读数——拿这段噪声当判据，
    库里就会多出一批凭空捏出来的战果。
    """
    assert parse_outcome(screens(BOTTOM_SAMPLE).outcome_banner()) is None


# -- 整条读法跑在实拍上 ------------------------------------------------------


def _read(name: str, bottom: str | None = None):  # type: ignore[no-untyped-def]
    from evo_helper.vision.live_reports import DETAIL_UI_VERSION, LiveReportReader
    from evo_helper.vision.models import PageObservation

    page = PageObservation(screen="mail_detail", ui_version=DETAIL_UI_VERSION, confidence=1.0)
    return LiveReportReader(screens(name)).read_detail_only(  # type: ignore[arg-type]
        page, bottom=screens(bottom) if bottom is not None else None
    )


def test_one_screen_alone_now_yields_a_verdict_from_the_banner() -> None:
    """`104400` 那张战损读不到、算式一律给 None——**但横幅读得出 `FAIL`**。

    这正是 2026-08-17 换判据的收益，而且是在真像素上兑现的：同一张图在
    2026-08-11 那版里 `outcome` 是 None，攻击日志的战果列只能显示「待战报」。
    """
    report = _read("dump-probe-report-unreadable-104400")

    assert report.raw_time_text == "11/08/2026 01:32:37"
    assert (report.defender.coordinate.value.system, report.defender.coordinate.value.position) == (
        320,
        11,
    )
    assert report.defender_units == 319
    # 战损照旧读不到——这一屏上根本没有那一行，横幅不替它编一个数出来。
    assert report.attacker_losses is None
    assert report.outcome == OUTCOME_FAIL


def test_two_real_screens_split_the_work_the_way_the_loop_does() -> None:
    """没拖过那屏出身份与横幅，拖到底那屏出「单位」与「损失单位」。

    ⚠️ **两张图不是同一份报告**——bot 战报拖到底之后那一屏至今没有实拍。
    所以这一条证明的是「两屏各司其职」，不是那些数属于这份 bot 战报。
    真正证明「必须拖」的是上面 `test_no_unscrolled_screen_yields_the_losses_at_all`。

    战果是 `FAIL`：第一屏的横幅这么写。拼进来的那四个数按算式会得出 `VICTORY`
    ——这正好把新的仲裁顺序钉死在真像素上，两条路打架时横幅赢。
    （两屏本来就不是同一份报告，算式那个 `VICTORY` 本来也不该采信。）
    """
    report = _read("dump-probe-report-unreadable-104251", bottom=BOTTOM_SAMPLE)

    assert (report.attacker_units, report.attacker_losses) == (100, 0)
    assert (report.defender_units, report.defender_losses) == (783, 783)
    assert report.outcome == OUTCOME_FAIL
