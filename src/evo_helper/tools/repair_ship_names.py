"""把已入库战报里的拉丁乱码舰种名换回中文。

为什么会有这种行数据：读舰队列时名称走 `chi_sim`、数量走 `eng`，两遍行数对不上
就整列退回英文那遍——于是 `轻型战斗机` 存成了 `SRLS HL`、`重型战斗机` 存成 `BHR`。
数量是对的，所以从头到尾没有任何报错。读取侧已修（`report_screens._read_fleet`
再也不会交出英文名），但**存量数据不会自己变好**，需要这个工具回填。

做法：拿这份战报的回放截图，按行位置重读一遍名称列（`chi_sim` + 词表吸附），
再按 rowid 顺序对位替换。三条自我约束：

- **只改名，不改数量。** 数量已经过合计校验，重读数字只会引入新的错误。
- **只改对不上词表的名字。** 已经是中文的行不碰。
- **行数对不上就整组拒绝。** 少一行就会让后面每一行都错位，错位比乱码更难发现。

    python -m evo_helper.tools.repair_ship_names --replay var/logs/vp-replay.png \\
        --report 08/08/2026 --apply
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import literal_column, select
from sqlalchemy.orm import Session

from evo_helper.config import Settings
from evo_helper.storage import models as orm
from evo_helper.storage.database import create_database_engine, create_session_factory
from evo_helper.vision.parsers import snap_unit_name
from evo_helper.vision.report_layout import crop_to_viewport, layout_for_viewport

#: 参战区在 `locate_sections` 的结果里排第一，其后依次是各回合。
PARTICIPATING = None


class Rename(NamedTuple):
    """一行的改名意图。

    ``row`` 直接拿 ORM 对象，不存 id 字符串：主键是 UUID，`session.get` 用
    `str(uuid)`（带连字符）取不到 CHAR(32) 的行，于是 `--apply` 会**一声不响地
    什么都不改**——命令打印出一整屏「已写库」，库里纹丝不动。

    字段叫 ``quantity`` 不叫 ``count``：``NamedTuple`` 继承自 ``tuple``，
    ``count`` 是元组自带的方法名，占用它会让类型检查直接报错。
    """

    row: orm.FleetSnapshotRow
    side: str
    round_no: int | None
    before: str
    after: str
    quantity: int


def needs_repair(name: str) -> bool:
    """名字落不到已知词表上，就是需要修的那种行。"""
    return snap_unit_name(name)[1] == "unknown"


def read_section_names(
    image: object, layout: object, band: object, top: int, bottom: int, tesseract_cmd: str
) -> list[str]:
    """按行位置重读一列的舰种名，逐个吸附到词表；吸附不上的行原样返回。"""
    from evo_helper.vision.optional.report_screens import ImageReportScreens

    screens = ImageReportScreens(image, layout, tesseract_cmd=tesseract_cmd)  # type: ignore[arg-type]
    found = screens._fleet_names(band, top, bottom)  # type: ignore[arg-type]
    if not found:
        return []
    _pitch, rows = found
    return [snap_unit_name(label)[0] for _y, label in rows]


def in_catalogue_order(names: Sequence[str]) -> bool:
    """名单是否按游戏目录顺序排列：先舰船（`SHIP_ORDER`），后防御设施。

    这是对位替换唯一的结构性旁证。游戏列舰队时严格按目录顺序，所以一份读得对的
    名单必然是目录的子序列；顺序乱了说明行位置本身就读错了，这时候按位置替换
    只会把乱码换成**另一个舰种的名字**——那比乱码危险得多，因为它看着是对的。
    """
    from evo_helper.vision.parsers import DEFENCE_ORDER, SHIP_ORDER

    catalogue = [*SHIP_ORDER, *DEFENCE_ORDER]
    cursor = -1
    for name in names:
        if name not in catalogue:
            return False
        index = catalogue.index(name)
        if index <= cursor:
            return False
        cursor = index
    return True


def plan_renames(
    stored: Sequence[orm.FleetSnapshotRow], recovered: Sequence[str]
) -> list[Rename] | None:
    """按位置生成改名清单；对不上就返回 None（整组拒绝，不做部分替换）。

    ``recovered`` 可能**比入库的行多**：入库那次是按英文那遍的行数截断的，
    尾部读不出数字的行（实测 `火箭发射器` 被读成 `KGS a9`）当场就丢了。
    所以这里按**前缀**对位，多出来的尾行由调用方打印出来供人核对。

    前缀对位的前提是「丢的是尾行，不是中间行」。这一点由 `in_catalogue_order`
    把关：名单必须整体落在游戏目录顺序上，中间掉一行不会破坏顺序，
    但**行位置读错**一定会——而后者才是会把 A 舰种改成 B 舰种的那种事故。
    """
    if len(recovered) < len(stored):
        return None
    if not in_catalogue_order(recovered):
        return None
    renames: list[Rename] = []
    for row, name in zip(stored, recovered[: len(stored)], strict=True):
        if not needs_repair(row.ship_type):
            continue
        if needs_repair(name):
            # 重读出来的也认不出，换过去没有意义，还会掩盖问题。
            continue
        renames.append(
            Rename(
                row=row,
                side=row.side,
                round_no=row.round_no,
                before=row.ship_type,
                after=name,
                quantity=row.count,
            )
        )
    return renames


def _stored_rows(
    session: Session, report_id: object, side: str, round_no: int | None
) -> list[orm.FleetSnapshotRow]:
    """按 rowid 取一组行——rowid 顺序就是当初的解析顺序，也就是画面上的行序。"""
    query = select(orm.FleetSnapshotRow).where(
        orm.FleetSnapshotRow.report_id == report_id,
        orm.FleetSnapshotRow.side == side,
    )
    query = query.where(
        orm.FleetSnapshotRow.round_no.is_(None)
        if round_no is None
        else orm.FleetSnapshotRow.round_no == round_no
    )
    # SQLite 的隐式 `rowid` 按插入顺序递增，而插入顺序就是解析顺序、也就是画面行序。
    # 主键是 uuid4 十六进制串，按它排等于随机排——对位替换会整组错位。
    return list(session.scalars(query.order_by(literal_column("rowid"))).all())


def _find_report(session: Session, needle: str) -> orm.BattleReportRow:
    reports = list(session.scalars(select(orm.BattleReportRow)).all())
    matches = [
        report
        for report in reports
        if needle in (report.raw_time_text or "") or needle == str(report.id)
    ]
    if not matches:
        raise SystemExit(f"没有匹配 {needle!r} 的战报；库里有 {len(reports)} 份")
    if len(matches) > 1:
        raise SystemExit(f"{needle!r} 匹配到 {len(matches)} 份战报，请给出更精确的时间或 id")
    return matches[0]


def build_parser() -> argparse.ArgumentParser:
    """`--tesseract` 的默认值从配置读，不写死。

    在这里读（而不是模块级常量）是有意的：常量在 import 那一刻就定死了，
    `.env` 或环境变量之后再改都不生效，而那种不生效不报错。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True, help="这份战报的回放截图")
    parser.add_argument(
        "--report", required=True, help="战报 id，或时间文本的一段（如 08/08/2026）"
    )
    parser.add_argument("--tesseract", default=Settings().tesseract_path)
    parser.add_argument("--apply", action="store_true", help="真的写库；不给就只打印")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.replay.is_file():
        parser.error(f"截图不存在: {args.replay}")

    from PIL import Image

    from evo_helper.vision.optional.report_screens import locate_sections

    # 先裁掉 Chrome --app 那条 38px 标题栏：整窗截图是 1920x917，版面标定是 1920x879。
    image = crop_to_viewport(Image.open(args.replay))
    layout = layout_for_viewport(image.width, image.height)
    sections = locate_sections(image, layout)
    if not sections:
        raise SystemExit("回放截图里定位不到分节横幅，无法按行重读")

    engine = create_database_engine(Settings().database_url)
    session_factory = create_session_factory(engine)

    renames: list[Rename] = []
    refused: list[str] = []
    with session_factory() as session:
        report = _find_report(session, args.report)
        print(f"战报 {report.id}  {report.raw_time_text}")
        for index, (top, bottom) in enumerate(sections):
            round_no = PARTICIPATING if index == 0 else index
            label = "参战" if round_no is None else f"第{round_no}回合"
            for side, band in (
                ("attacker", layout.attacker_column),
                ("defender", layout.defender_column),
            ):
                stored = _stored_rows(session, report.id, side, round_no)
                if not stored:
                    continue
                raw = read_section_names(image, layout, band, top, bottom, args.tesseract)
                # 认不出的行是装饰性噪声（实测：孤零零一行 `”`、一行 `1 17`）。
                # 打印出来而不是默默丢——丢掉一行真数据会让后面每一行错位。
                recovered = [name for name in raw if not needs_repair(name)]
                dropped = [name for name in raw if needs_repair(name)]
                if dropped:
                    refused.append(f"{label} {side}: 重读时丢弃认不出的行 {dropped}")
                planned = plan_renames(stored, recovered)
                if planned is None:
                    refused.append(
                        f"{label} {side}: 入库 {len(stored)} 行、重读 {len(recovered)} 行，"
                        "对不上或顺序不符合目录，整组跳过"
                    )
                    continue
                renames.extend(planned)

        for line in refused:
            print(f"  [跳过] {line}")
        for item in renames:
            where = "参战" if item.round_no is None else f"第{item.round_no}回合"
            print(
                f"  {where} {item.side}: {item.before}  ->  {item.after}"
                f"   (数量 {item.quantity} 不变)"
            )

        if not renames:
            print("没有需要改名的行。")
            return 0
        if not args.apply:
            print(f"\n预演：{len(renames)} 行可改名。加 --apply 才会写库。")
            return 0

        for item in renames:
            item.row.ship_type = item.after
        session.commit()
        print(f"\n已写库：{len(renames)} 行。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
