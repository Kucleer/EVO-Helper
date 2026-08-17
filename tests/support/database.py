"""测试用的数据库地址：CI 上是 PostgreSQL，本地默认仍是 SQLite。

## 为什么要有这一层

生产 2026-08-16 全面切到 PostgreSQL，而测试当时仍然全跑 SQLite。同一天
`/planets` 整页 500 就是从这个缺口漏过去的：分类计数 `SELECT` 了一个没进
`GROUP BY` 的列，**SQLite 容忍、PostgreSQL 报 `GroupingError`**，226 条相关用例
在 SQLite 上一条都没红。只要测试还跑另一种方言，这类缺陷就会继续溜进生产，
而且下一个不一定像整页 500 那么扎眼——可能只是某个统计数字悄悄算错。

于是 CI 起一个 postgres 服务、把 `EVO_HELPER_TEST_DATABASE_URL` 指过去，全量
测试就跑在与生产同一种方言上。本地不设这个变量时仍然落回 SQLite：迭代快，
而真正的把关在 CI。

## 隔离方式：一个测试一个 schema，不是一个数据库

`CREATE DATABASE` 每次要一两百毫秒，几十个建库点乘下来很可观；`CREATE SCHEMA`
是毫秒级。schema 通过连接串里的 `options=-csearch_path=...` 生效，所以
`create_database_engine(url)` 和 `alembic upgrade`（连 `alembic_version` 表）
都会落在这个 schema 里，调用方一行都不用改。

⚠️ **schema 名由「路径 + 文件名」决定，是确定的，不是随机的。** 有用例靠
「用同一个文件路径再开一次引擎」来证明数据落了盘（`test_persistent_web_service`
的重启用例就是），随机命名会让那种用例每次都拿到一个空库——变成永远绿的假证明。

## 跑完要清理

schema 不会自己消失（跑到一半被 Ctrl+C 更是直接留下一地）。CI 上无所谓——服务容器
跑完就销毁；对着一个长期存在的测试库跑就得手动清一次：

    python tests/support/database.py

⚠️ 函数名**刻意不叫 `test_database_url`**：那个名字会被 pytest 当成测试函数，从
每一个 import 它的模块里各收走一条，用例数凭空虚涨、警告成片。
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from sqlalchemy import create_engine, text

#: 指向一个可写的 PostgreSQL 时，全部测试库都建在它里面。CI 设它，本地一般不设。
TEST_DATABASE_URL_VAR = "EVO_HELPER_TEST_DATABASE_URL"

#: schema 名前缀。清理与识别都靠它，别改成别的前缀而不同步 `drop_test_schemas`。
SCHEMA_PREFIX = "evotest_"

_created: set[str] = set()


def scratch_database_url(tmp_path: Path, name: str = "test.db") -> str:
    """给这个测试一个干净的库地址。

    `name` 的作用和 SQLite 下的文件名完全一样：同一个 `tmp_path` 里两个不同的
    `name` 是两个库，同一个 `name` 是同一个库。
    """
    base = os.environ.get(TEST_DATABASE_URL_VAR)
    if not base:
        return f"sqlite:///{tmp_path / name}"
    return _postgres_url(base, _schema_for(tmp_path, name))


def _schema_for(tmp_path: Path, name: str) -> str:
    # 路径本身可能很长且带盘符/反斜杠，PG 的标识符上限是 63 字节，所以取摘要。
    digest = hashlib.sha1(f"{tmp_path.as_posix()}/{name}".encode()).hexdigest()[:24]
    return f"{SCHEMA_PREFIX}{digest}"


def _postgres_url(base: str, schema: str) -> str:
    _ensure_schema(base, schema)
    separator = "&" if "?" in base else "?"
    # `-c search_path=...` 由 libpq 在连接时应用，所以引擎、Alembic、以及任何
    # 直接拿这个串开连接的代码都会看到同一个 schema。
    #
    # ⚠️ **里面那个 `=` 不要百分号转义。** 查询串是按第一个 `=` 切键值的，
    # `options=-csearch_path=xxx` 本来就解析得对；而转义成 `%3D` 会引进一个 `%`，
    # Alembic 的 `Config` 是 ConfigParser，拿到带 `%` 的值直接
    # `ValueError: invalid interpolation syntax`——几条迁移用例就是这么红的。
    return f"{base}{separator}options=-csearch_path={schema}"


def _ensure_schema(base: str, schema: str) -> None:
    if schema in _created:
        return
    engine = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    finally:
        engine.dispose()
    _created.add(schema)


def drop_test_schemas(base: str | None = None) -> int:
    """删掉本前缀下的全部 schema，返回删了几个。

    整轮跑完调一次即可。**不按 `_created` 删**：上一轮被 Ctrl+C 打断留下的残骸
    不在这个集合里，而它们会一直堆在库里。
    """
    target = base or os.environ.get(TEST_DATABASE_URL_VAR)
    if not target:
        return 0
    dropped = 0
    engine = create_engine(target, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            names = [
                row[0]
                for row in connection.execute(
                    text(
                        "SELECT schema_name FROM information_schema.schemata "
                        "WHERE schema_name LIKE :prefix"
                    ),
                    {"prefix": f"{SCHEMA_PREFIX}%"},
                )
            ]
            for name in names:
                # 名字来自我们自己的前缀查询，但仍然只放行已知形状——
                # 拼进 DDL 的标识符不该有任何一条路径来自外部输入。
                if not re.fullmatch(rf"{SCHEMA_PREFIX}[0-9a-f]{{24}}", name):
                    continue
                connection.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
                dropped += 1
    finally:
        engine.dispose()
    _created.clear()
    return dropped


if __name__ == "__main__":  # pragma: no cover - 手动清理入口
    print(f"dropped {drop_test_schemas()} schema(s)")
