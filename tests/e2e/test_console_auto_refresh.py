"""攻击日志与系统日志每 15 秒自己更新一次，而且**关得掉、后台不空转**。

用户口径（2026-08-17）：「攻击/系统日志页面增加 15 秒自动刷新」。

这个文件钉的是四件互相制衡的事，缺一件另外几件就会走样：

1. **两页上都有那个开关，而且默认开着。** 没有开关的自动刷新在「我正在逐行读
   一段旧日志」的时候是纯干扰。
2. **页面不可见时真的停表。** 这两页会被挂在后台一整夜——不停轮询是白烧带宽，
   而每一次都要走一遍 SQL。判据是 `visibilityState` / `visibilitychange`，
   不是「发了再丢掉」。
3. **上一次没回来就不发下一次。** 库忙时一次查询可能超过 15 秒，不挡的话请求
   越堆越多，把本来就慢的库压得更慢。
4. **刷新保住筛选。** 两页的筛选与翻页全部走 URL 查询参数，重取的就是当前
   地址；而页面上跟着换的只有标了 `data-refresh` 的数据块，筛选表单**不在
   里面**——刷掉用户正在填的筛选比不刷还烦。

⚠️ **这里只查得到「代码里写着这么做」，查不到「浏览器真的这么跑」**：这套仓库
没有 JS 运行时。所以下面几条刻意钉在**很难被顺手改掉**的位置上（共用那一份
实现里的判据本身），而不是「页面上有 auto-refresh 这个词」——后者删掉可见性
暂停之后照样是绿的。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

import evo_helper.web
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.web.app import create_persistent_app

#: 两页共用同一份实现，所以两页都要过同一组断言。
AUTO_REFRESH_PAGES = ["/logs", "/system-log"]

#: 共用实现所在的模板。断言直接读它，不经过渲染——那几条判据（可见性、并发）
#: 在任何一页的 HTML 里长得都一样，读源文件才说得清「只有一份」。
BASE_TEMPLATE = Path(evo_helper.web.__file__).parent / "templates" / "base.html"


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'auto-refresh.db'}")
    Base.metadata.create_all(engine)
    factory: sessionmaker[Session] = create_session_factory(engine)
    # 刻意不走 `with TestClient(...)`：那会跑 lifespan，而 lifespan 会把常驻
    # 调度器叫起来——这一组用例只看页面长什么样，不需要、也不该起调度。
    started = TestClient(create_persistent_app(factory, local_token="test-token"))
    started.headers.update({"X-Evo-Helper-Token": "test-token"})
    yield started
    engine.dispose()


def _shared_script() -> str:
    return BASE_TEMPLATE.read_text(encoding="utf-8")


# -- 开关 ----------------------------------------------------------------------


@pytest.mark.parametrize("url", AUTO_REFRESH_PAGES)
def test_the_page_carries_a_switch_that_is_on_by_default(client: TestClient, url: str) -> None:
    """开关必须在页面上。默认开着，但**必须是一个复选框**——关不掉的自动刷新
    会在用户逐行读旧日志时不停把内容换掉。"""
    body = client.get(url).text

    assert '<input type="checkbox" id="auto-refresh" checked>' in body
    assert "每 15 秒自动刷新" in body


@pytest.mark.parametrize("url", AUTO_REFRESH_PAGES)
def test_the_page_wires_itself_to_the_shared_implementation(client: TestClient, url: str) -> None:
    """两页都调同一个共用入口，谁也不许自己抄一份。

    抄第二份的那天，可见性暂停、并发保护、保住筛选这三条就会各改各的，
    而分家之后只有其中一页会转红。
    """
    body = client.get(url).text

    assert "EVOHelper.autoRefresh();" in body


@pytest.mark.parametrize("url", AUTO_REFRESH_PAGES)
def test_the_page_marks_the_data_block_but_not_the_filter_form(
    client: TestClient, url: str
) -> None:
    """跟着刷新的只有数据块；筛选表单在它前面，不在它里面。

    ⚠️ 按**出现次序**判：两页的筛选表单都排在数据块之前，所以「第一个
    `data-refresh` 出现在最后一个 `<form` 之后」就等于「表单一个都没被圈进去」。
    把整块 `<section>` 或者整页圈进 `data-refresh`，这一条立刻转红。

    ⚠️ **先把 `<head>` 切掉**：共用实现自己就要写 `[data-refresh]` 这个选择器，
    留着它，第一个出现的位置永远是 `<head>` 里的那一个，这一条就永远是绿的。
    """
    body = client.get(url).text.split("</head>", 1)[-1]

    assert "data-refresh" in body, "这一页没有任何跟着刷新的数据块"
    assert body.rindex("<form") < body.index("data-refresh"), (
        "筛选表单落在了自动刷新的范围里；刷新会把用户正在填的筛选顶掉"
    )


# -- 共用实现里的三条判据 ------------------------------------------------------


def test_the_refresh_period_is_fifteen_seconds() -> None:
    assert "AUTO_REFRESH_MS: 15000," in _shared_script()


def test_it_stops_polling_while_the_page_is_hidden() -> None:
    """页面不可见时**两道**都要在：定时器停掉，且已排上的那一次也不发。

    只留一道是不够的：光判 `visibilitychange` 而不判 `visibilityState`，
    切回来那一瞬间排上的定时器仍会在下一次隐藏后打出去；光判
    `visibilityState` 而不停表，则每 15 秒仍在唤醒一次页面。
    """
    script = _shared_script()

    assert "document.visibilityState === 'visible'" in script, "少了「页面可见吗」这条判据"
    assert "visibilitychange" in script, "没有在可见性变化时重新算表"
    assert "window.clearInterval(timer)" in script, "隐藏时没有真的把定时器停掉"
    assert "if (inFlight || !toggle.checked || !visible()) return;" in script, (
        "隐藏时仍会发出请求：这一次刷新没有在开头就被可见性挡下"
    )


def test_it_never_has_two_requests_in_flight() -> None:
    script = _shared_script()

    assert "let inFlight = false;" in script
    assert "inFlight = true;" in script
    assert "inFlight = false;" in script


def test_it_refetches_the_current_url_so_filters_and_paging_survive() -> None:
    """重取的是 `window.location.href`。

    两页的筛选与翻页全部走 URL 查询参数，所以照当前地址取一遍拿回来的就是
    同一份筛选、同一页。写死 `/logs` 会把用户选的筛选和翻到的页一起丢掉。
    """
    script = _shared_script()

    assert "fetch(window.location.href," in script
