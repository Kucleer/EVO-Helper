"""把 SQLite 库整库搬到 PostgreSQL。**只读源库，只写目标库，不删任何东西。**

    python tools/migrate_sqlite_to_pg.py --source sqlite:///var/evo-helper.db --target <pg url>
    python tools/migrate_sqlite_to_pg.py ... --verify-only     # 只比行数，不写

⚠️ **走 SQLAlchemy 的元数据，不裸拷字节。** 两边的类型表示完全不同：

    UUID       SQLite 存成 32 位十六进制字符串 / BLOB，PG 是原生 uuid
    布尔       SQLite 是 0/1 整数，PG 是 true/false
    时刻       `UTCDateTime` 在 SQLite 里是字符串，PG 里是 timestamptz

裸 `INSERT ... SELECT` 或者 CSV 中转都要把这些转换重写一遍，而**写错了不会报错**
——UUID 会变成一串看着像 UUID 的文本，时刻会丢时区。让每一列都经过它自己那个
`TypeDecorator` 是唯一不用重写转换逻辑的办法。

⚠️ **表的顺序由外键决定**，用 `metadata.sorted_tables`（SQLAlchemy 算好的拓扑序）。
自己按字母序插会撞外键，而撞了之后一半的表已经进去了，回滚起来比重来还麻烦。

⚠️ **自增主键的序列要单独校准。** PG 的 identity/serial 有一个独立的计数器，
批量插入指定 id 时它**不会跟着走**。不校准的话，迁移完之后第一次新增就撞主键冲突
——而那时你已经在用新库了。
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine, func, insert, select, text
from sqlalchemy.engine import Engine

from evo_helper.storage import models  # noqa: F401 - 导入才会把表注册进 Base.metadata
from evo_helper.storage.database import Base
from evo_helper.tools.scan_coordinates import make_console_encoding_safe

#: 不搬的表。`alembic_version` 由 `alembic upgrade head` 自己写对，
#: 搬过去反而可能盖成旧版本。
SKIP = frozenset({"alembic_version"})

#: 一次插多少行。太大在网络往返上省不了多少，反而让一次失败要重来更多。
CHUNK = 500


def _say(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def counts(engine: Engine) -> dict[str, int]:
    """每张表几行。**只读。**"""
    out: dict[str, int] = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name in SKIP:
                continue
            out[table.name] = conn.execute(select(func.count()).select_from(table)).scalar_one()
    return out


def copy_table(source: Engine, target: Engine, table) -> int:  # type: ignore[no-untyped-def]
    """搬一张表，返回搬了几行。目标表非空就跳过（幂等重跑用）。"""
    with target.connect() as check:
        if check.execute(select(func.count()).select_from(table)).scalar_one():
            return -1

    moved = 0
    with source.connect() as read, target.begin() as write:
        rows = read.execute(select(table))
        while True:
            batch = rows.fetchmany(CHUNK)
            if not batch:
                break
            write.execute(insert(table), [dict(row._mapping) for row in batch])
            moved += len(batch)
    return moved


def _is_integer_column(column) -> bool:  # type: ignore[no-untyped-def]
    """这一列是不是整数。

    `python_type` 对少数类型会抛 `NotImplementedError`；抛了就当它不是整数——
    那种列本来也不会有自增序列，`pg_get_serial_sequence` 会返回 NULL。
    """
    try:
        return column.type.python_type is int
    except NotImplementedError:
        return False


def resync_sequences(target: Engine) -> list[str]:
    """把自增主键的序列推到当前最大值之后。

    ⚠️ 不做这一步的后果是**迁移完之后第一次新增就撞主键冲突**——而那时
    旧库已经不用了，现场很难看。PG 的 identity 计数器不会因为你插了指定 id 就前进。
    """
    fixed: list[str] = []
    with target.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name in SKIP:
                continue
            for column in table.primary_key.columns:
                if not _is_integer_column(column):
                    continue
                sequence = conn.execute(
                    text("select pg_get_serial_sequence(:t, :c)"),
                    {"t": table.name, "c": column.name},
                ).scalar()
                if sequence is None:
                    continue
                conn.execute(
                    text(
                        f"select setval('{sequence}', "
                        f"coalesce((select max({column.name}) from {table.name}), 0) + 1, false)"
                    )
                )
                fixed.append(f"{table.name}.{column.name}")
    return fixed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--verify-only", action="store_true", help="只比两边行数，一个字都不写")
    args = parser.parse_args(argv)

    make_console_encoding_safe()
    source = create_engine(args.source)
    target = create_engine(args.target)

    before = counts(source)
    _say(f"源库 {len(before)} 张表，共 {sum(before.values()):,} 行")

    if not args.verify_only:
        for table in Base.metadata.sorted_tables:
            if table.name in SKIP:
                continue
            moved = copy_table(source, target, table)
            if moved == -1:
                _say(f"  {table.name:34} 目标已有数据，跳过")
            elif moved:
                _say(f"  {table.name:34} {moved:>8,} 行")
        fixed = resync_sequences(target)
        _say(f"校准自增序列 {len(fixed)} 个")

    after = counts(target)
    _say("")
    bad = [name for name, n in before.items() if after.get(name, -1) != n]
    for name in sorted(set(before) | set(after)):
        mark = "  " if name not in bad else "✗ "
        _say(f"{mark}{name:34} 源 {before.get(name, 0):>8,}   目标 {after.get(name, 0):>8,}")
    _say("")
    if bad:
        _say(f"[!] {len(bad)} 张表行数对不上：{bad}")
        return 1
    _say(f"[OK] 全部对上，共 {sum(after.values()):,} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
