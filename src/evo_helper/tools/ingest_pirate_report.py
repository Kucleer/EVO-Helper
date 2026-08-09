"""把一份海盗攻击报告读进库：只要胜负与战损总数。

对游戏只读——输入是已经拍好的截图，不是活的浏览器。

需要**两张**详情页截图（同一封报告）：

- `--detail`：刚打开、没滚动那一屏。给出主题、时间、VS 块、`VICTORY`/`FAIL`、「单位」总数
- `--bottom`：面板拖到底那一屏。给出「损失单位」总数

为什么要两张：未滚动时「损失单位」正好被面板下沿切掉，而拖到底之后
`VICTORY` 横幅又滚出了可视区。两样东西不在同一屏上。

拖到底的姿势（实机 2026-08-09）：在面板里从 y≈700 慢拖到 y≈300。
**必须是慢拖**（按下 → 分步移动 → 松开）：一步到位的 `dragTo` 会被面板
当成点击，同样的起止点有时滚有时不滚。

    python -m evo_helper.tools.ingest_pirate_report \
        --detail var/logs/pir1-detail.png --bottom var/logs/pir1-bottom.png
"""

from __future__ import annotations

import argparse
from pathlib import Path
from uuid import uuid4

from evo_helper.application.report_ingest import to_pirate_battle_report
from evo_helper.config import Settings
from evo_helper.infrastructure.logging import configure_logging
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.storage.repository import SqlAlchemyRepository
from evo_helper.vision.pirate_reports import PirateReportReading, read_pirate_report
from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport

DEFAULT_TESSERACT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _screens(path: Path, tesseract_cmd: str) -> object:
    from PIL import Image

    from evo_helper.vision.optional.report_screens import ImageReportScreens

    # 整窗截图带着 Chrome --app 那条 38px 标题栏（1920×917），版面标定是裁掉它
    # 之后的 1920×879。不裁就一行都读不出来。
    image = crop_to_viewport(Image.open(path))
    return ImageReportScreens(
        image,
        layout_for_viewport(image.width, image.height),
        tesseract_cmd=tesseract_cmd,
    )


def read_report(detail: Path, bottom: Path, tesseract_cmd: str) -> PirateReportReading:
    """两屏各一个 `ImageReportScreens`——同一个实例读两屏会把上一屏的像素当成这一屏。"""
    return read_pirate_report(
        _screens(detail, tesseract_cmd),  # type: ignore[arg-type]
        _screens(bottom, tesseract_cmd),  # type: ignore[arg-type]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--detail", type=Path, required=True, help="未滚动的详情页截图")
    parser.add_argument("--bottom", type=Path, required=True, help="拖到底的详情页截图")
    parser.add_argument("--tesseract", default=DEFAULT_TESSERACT)
    parser.add_argument("--dry-run", action="store_true", help="只打印读数，不写库")
    args = parser.parse_args(argv)
    log_path = configure_logging()

    for path in (args.detail, args.bottom):
        if not path.is_file():
            parser.error(f"截图不存在：{path}")

    reading = read_report(args.detail, args.bottom, args.tesseract)
    report = to_pirate_battle_report(reading, report_id=uuid4())

    print(f"报告时间（原文）: {reading.raw_time_text}")
    print(f"报告时间（UTC） : {reading.reported_at_utc.isoformat()}")
    print(f"出发            : {reading.attacker_name} {reading.attacker_origin}")
    print(f"目标            : {reading.defender_name} {reading.defender_target}")
    print(f"胜负            : {reading.outcome}")
    print(f"战损（我方/对方）: {reading.attacker_losses} / {reading.defender_losses}")
    print(f"单位（我方/对方）: {reading.attacker_units} / {reading.defender_units}")
    print(f"日志            : {log_path}")
    if args.dry_run:
        print("dry run：没有写库")
        return 0

    settings = Settings()
    session_factory = create_session_factory(create_database_engine(settings.database_url))
    SqlAlchemyRepository(session_factory).append_report(report)
    target = reading.defender_target
    print(f"已入库 {report.report_id}")
    print(f"查看: /targets/{target.galaxy}:{target.system}:{target.position}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
