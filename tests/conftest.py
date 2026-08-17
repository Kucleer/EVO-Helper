"""跑 PostgreSQL 那一轮时，每条用例结束后把它用过的引擎连接还回去。

## 为什么需要这一条

绝大多数用例是 `engine = create_database_engine(...)` 之后就不管了——从来不
`dispose()`。SQLite 上这没有代价：引擎被回收时那个文件句柄跟着关掉，早一点晚一点
都一样。PostgreSQL 上代价是**服务器端的一个连接**，而且引擎多半卡在引用环里
（引擎 ↔ 连接池 ↔ 方言，再加上 FastAPI 应用与 `TestClient` 那一层），靠引用计数
回收不掉，只能等分代 GC 恰好扫到。

于是连接数随用例数单调上涨。实测：什么都不做约 0.8 个/用例，跑一百多条就把
`max_connections`（默认 100）占满，后面的用例成片倒在
`FATAL: sorry, too many clients already`；从哪条开始红全看运气，看起来像 flaky，
和真正的方言问题混在一起根本分不开。**改成每条用例跑完强制 `gc.collect()` 也只是
把速度降到约 0.15 个/用例——照样撑不完一整轮。**

所以这里不赌 GC：`engine_connect` 是引擎级事件，任何引擎只要开过连接就会被记下，
用例拆完挨个 `dispose()`。`dispose()` 只关**已经还回池子里的**连接，还借在外面的
那些交给原本的回收路径，所以不会把跨用例活着的引擎弄坏——它下次用时自己重连。

⚠️ **只在 PostgreSQL 那一轮做。** SQLite 那一轮没有这个问题，白白多做一遍
`dispose()` 只是给另一种方言加税。

环境变量名和 `tests/support/database.py` 里的 `TEST_DATABASE_URL_VAR` 是同一个。
这里写字面量而不是 import 它：conftest 的加载时机在 `pythonpath` 生效与否的边界上，
为了一个字符串去赌那个顺序不值得。
"""

from __future__ import annotations

import os
import weakref

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine

_ON_POSTGRES = bool(os.environ.get("EVO_HELPER_TEST_DATABASE_URL"))

#: 本条用例期间开过连接的引擎。弱引用：这里不该是让引擎活下去的那个理由。
_engines: weakref.WeakSet[Engine] = weakref.WeakSet()


@event.listens_for(Engine, "engine_connect")
def _remember_engine(connection: Connection) -> None:
    if _ON_POSTGRES:
        _engines.add(connection.engine)


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown() -> None:
    """跑在所有 fixture 拆完之后，这样这条用例建的引擎已经没人在用了。"""
    if not _ON_POSTGRES:
        return
    for engine in list(_engines):
        engine.dispose()
    _engines.clear()
