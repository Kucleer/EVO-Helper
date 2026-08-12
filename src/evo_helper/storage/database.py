"""SQLite storage bootstrap with WAL, foreign keys, and UTC-aware timestamps."""

from __future__ import annotations

from datetime import UTC, datetime
from sqlite3 import Connection as SQLiteConnection

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import DateTime, TypeDecorator


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator[datetime]):
    """带时区的 UTC 时刻：写入拒绝 naive，读出一律归一到 UTC。

    ``impl`` **必须**带 ``timezone=True``，且绑定值**必须保留 tzinfo**。这两件事
    是一体的，缺一处都会在 Postgres 上安静地错：

    - ``timezone=True`` 让列在 Postgres 上是 ``TIMESTAMP WITH TIME ZONE``。
      没有它就是 ``TIMESTAMP WITHOUT TIME ZONE``，tzinfo 被**静默截掉**，
      读回来变成 naive——本项目已经被时区坑过三次（战报页眉当 UTC+8 解析、
      ``--round-started-at`` 不带时区、攻击日志按 UTC+8 切日），每一条判据都建立在
      「读出来是 aware 的 UTC」上。
    - 反过来，列成了 ``TIMESTAMPTZ`` 却还像从前那样 ``replace(tzinfo=None)``
      再交出去，Postgres 会拿**会话时区**去解释这个 naive 值。服务器时区不是 UTC
      时整体偏几个小时，同样不报错。

    SQLite 上 ``timezone=True`` **不改变**建表类型（两者都是 ``DATETIME``），
    所以对应的 alembic 迁移在 SQLite 上是无操作。但 SQLite 方言的绑定处理会把偏移量
    **直接丢掉而不换算**（``03:04:05+08:00`` 存成 ``03:04:05``），所以这里的
    ``astimezone(UTC)`` 不是可有可无的规范化，它是 SQLite 上存对时刻的唯一保证。
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("business timestamps must be timezone-aware")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite（以及 Postgres 上尚未迁移的 naive 列）返回 naive 值。
            # 存进去的一律是 UTC 时刻，所以这里贴 UTC 标签而不是换算。
            return value.replace(tzinfo=UTC)
        # Postgres 的 TIMESTAMPTZ 返回的是**会话时区**下的 aware 值，
        # 必须换算而不是 replace——replace 会把 20:00+08:00 谎报成 20:00+00:00。
        return value.astimezone(UTC)


def create_database_engine(database_url: str) -> Engine:
    """Create an engine, enabling WAL and foreign keys for SQLite URLs."""
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        _enable_sqlite_pragmas(engine)
    return engine


def _enable_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: SQLiteConnection, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
