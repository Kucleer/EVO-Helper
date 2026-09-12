"""`/api/system-log` 与 `/system-log` 页面。

⚠️ 这一页**不是** `/logs`。那一页是「攻击日志」，读的是
`attack_intents ⟕ attack_dispatches ⟕ battle_reports`。这里顺带钉住这一点：
两条路由都还在，各读各的表。
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from evo_helper.infrastructure.system_log import SystemLogRecord
from evo_helper.storage.database import Base, create_database_engine, create_session_factory
from evo_helper.storage.system_log import SystemLogRepository
from evo_helper.web.app import create_persistent_app
from support.database import scratch_database_url

BASE_TIME = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)

#: 一张「现场图」，量级同实机那批（生产库里 8 万到 16 万字符的 base64）。
#: 用真字节而不是一串 `A`：`GET /system-log/{id}/image` 要把它解回来原样返回，
#: 解不开的假数据验不出这件事。
SCREENSHOT_BYTES = b"\x89PNG\r\n\x1a\n" + b"pixels!" * 12_000
SCREENSHOT_BASE64 = base64.b64encode(SCREENSHOT_BYTES).decode("ascii")
SCREENSHOT_PAYLOAD = json.dumps(
    {"note": "画面认不出", "thumbnail_png_base64": SCREENSHOT_BASE64}, ensure_ascii=False
)


@pytest.fixture
def client(tmp_path):  # type: ignore[no-untyped-def]
    engine = create_database_engine(scratch_database_url(tmp_path, "system-log.db"))
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    _seed(session_factory)
    app = create_persistent_app(session_factory, local_token="test-token")
    client = TestClient(app)
    client.headers.update({"X-Evo-Helper-Token": "test-token"})
    return client


def _seed_payload(index: int) -> str:
    """第 1 条（那条 ERROR）带一张现场图——实机上正是这种行才带图。

    刻意挂在既有的那一条上而不是多播一行：本文件里好几条用例断言的是「共 4 条」
    和「最新那一条是哪句」。
    """
    if index == 0:
        return '{"coordinate": "2:137:1"}'
    if index == 1:
        return SCREENSHOT_PAYLOAD
    return "{}"


def _error_row_id(client) -> int:  # type: ignore[no-untyped-def]
    """带现场图那一条的 id。"""
    rows = client.get("/api/system-log?level=ERROR").json()["rows"]
    return int(rows[0]["id"])


def _seed(session_factory) -> None:  # type: ignore[no-untyped-def]
    rows = [
        ("live-pc", "tools.pirate_loop", "INFO", "pirate", "扫到 2:137:1"),
        ("live-pc", "tools.pirate_loop", "ERROR", "pirate", "简报认不出，安全地不派"),
        ("console-pc", "application.mission_scheduler", "INFO", None, "补认 3 份战报"),
        ("console-pc", "web.app", "WARNING", "bot", "调度器 tick 失败"),
    ]
    SystemLogRepository(session_factory).append(
        [
            SystemLogRecord(
                logged_at_utc=BASE_TIME + timedelta(minutes=index),
                level=level,
                source=source,
                host=host,
                pid=1000 + index,
                message=message,
                mission_kind=kind,
                payload_json=_seed_payload(index),
            )
            for index, (host, source, level, kind, message) in enumerate(rows)
        ]
    )


def test_the_api_returns_the_newest_first_with_a_server_side_total(client) -> None:  # type: ignore[no-untyped-def]
    body = client.get("/api/system-log").json()

    assert body["total"] == 4
    assert body["rows"][0]["message"] == "调度器 tick 失败"
    assert body["hosts"] == ["console-pc", "live-pc"]
    assert not body["has_more"]


def test_every_filter_is_honoured(client) -> None:  # type: ignore[no-untyped-def]
    assert client.get("/api/system-log?level=ERROR").json()["total"] == 1
    assert client.get("/api/system-log?host=live-pc").json()["total"] == 2
    assert client.get("/api/system-log?source=web.app").json()["total"] == 1
    assert client.get("/api/system-log?mission_kind=pirate").json()["total"] == 2
    assert client.get("/api/system-log?q=2:137").json()["total"] == 1


def test_a_time_window_narrows_the_page(client) -> None:  # type: ignore[no-untyped-def]
    body = client.get("/api/system-log?since=2026-08-16T12:02:00&until=2026-08-16T12:03:00").json()

    assert [row["message"] for row in body["rows"]] == ["调度器 tick 失败", "补认 3 份战报"]


def test_paging_walks_the_whole_set_without_repeats(client) -> None:  # type: ignore[no-untyped-def]
    first = client.get("/api/system-log?limit=2").json()
    second = client.get("/api/system-log?limit=2&offset=2").json()

    assert first["has_more"] and not second["has_more"]
    ids = [row["id"] for row in first["rows"] + second["rows"]]
    assert len(set(ids)) == 4


def test_blank_query_parameters_do_not_empty_the_page(client) -> None:  # type: ignore[no-untyped-def]
    """浏览器提交表单必然带上 `level=&host=`。当成「等于空串」就永远是 0 条。"""
    response = client.get("/api/system-log?level=&source=&host=&mission_kind=&q=&run_id=")

    assert response.status_code == 200
    assert response.json()["total"] == 4


def test_an_unknown_run_id_matches_nothing_rather_than_erroring(client) -> None:  # type: ignore[no-untyped-def]
    body = client.get(f"/api/system-log?run_id={uuid4()}").json()

    assert body["total"] == 0


def test_the_page_renders_with_its_own_nav_entry(client) -> None:  # type: ignore[no-untyped-def]
    response = client.get("/system-log")

    assert response.status_code == 200
    assert "系统日志" in response.text
    assert "简报认不出，安全地不派" in response.text
    # 导航里两条日志入口各是各的，别把「攻击日志」占了。
    assert 'href="/system-log"' in response.text
    assert 'href="/logs"' in response.text


def test_the_page_says_so_when_it_could_not_use_a_filter(client) -> None:  # type: ignore[no-untyped-def]
    """认不出的 run_id 照常渲染全部记录，但必须说清「没按它筛」。

    默默地不筛才是最坏的一种：用户会把下面那些行当成筛出来的结果。
    """
    response = client.get("/system-log?run_id=not-a-uuid")

    assert response.status_code == 200
    assert "run_id 不是合法 UUID" in response.text
    assert "简报认不出，安全地不派" in response.text


def test_a_broken_limit_does_not_turn_the_page_into_json(client) -> None:  # type: ignore[no-untyped-def]
    """手改链接写出 `?limit=` 时也要是一张页面，不是一页 422。"""
    assert client.get("/system-log?limit=999999").status_code == 200


# -- 现场图：列表页只标一个链接，字节点开再取 --------------------------------


def test_the_page_carries_no_base64_at_all(client) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **本组核心判据：一页 HTML 里不许有 base64。**

    原先那张图是 `<img src="data:image/png;base64,…">` 内联进 DOM 的，一张就是
    8 万到 16 万字符：生产库上默认页 0.5 MB、每页 1000 行时 1.6 MB（实测
    2026-09-09），而这一页还挂着 15 秒自动刷新。`loading="lazy"` 在这里没有用——
    字节已经在 HTML 里了。

    判据落在「那一大串字符在不在页面上」，不是「模板有没有调某个函数」。
    """
    response = client.get("/system-log")

    assert response.status_code == 200
    assert SCREENSHOT_BASE64[:60] not in response.text
    assert "data:image/png;base64" not in response.text
    assert "画面认不出" not in response.text, "那一段 payload 也超了限，同样不该内联"


def test_the_page_says_the_screenshot_is_there_and_links_to_it(client) -> None:  # type: ignore[no-untyped-def]
    """不内联不等于不说。有图这件事必须看得见，而且点得开。"""
    entry_id = _error_row_id(client)

    text = client.get("/system-log").text

    assert f'href="/system-log/{entry_id}/image"' in text
    assert "现场图" in text
    # 超限的那段 payload 也要有去处，且说清省掉的是多大一段。
    assert f'href="/system-log/{entry_id}/payload"' in text
    assert "payload 109 KB" in text, f"实际 {len(SCREENSHOT_PAYLOAD)} 字符"


def test_the_screenshot_itself_is_still_retrievable(client) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **图一张都不许丢。** 这个仓的硬要求是「出事时能只靠库里日志定位」，
    而列表页现在只有一个链接——链接点不开就等于证据丢了。"""
    response = client.get(f"/system-log/{_error_row_id(client)}/image")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == SCREENSHOT_BYTES, "要的是原图，不是缩过一道的"


def test_the_withheld_payload_is_still_retrievable_in_full(client) -> None:  # type: ignore[no-untyped-def]
    """`payload_json` 是排障的命根子：可以点开再取，不能取不到。"""
    response = client.get(f"/system-log/{_error_row_id(client)}/payload")

    assert response.status_code == 200
    assert response.text == SCREENSHOT_PAYLOAD
    assert "画面认不出" in response.text


def test_the_api_still_hands_over_the_whole_payload(client) -> None:  # type: ignore[no-untyped-def]
    """接口那一路**没有**跟着上限走：脚本要的就是整段，含那张图。"""
    rows = client.get("/api/system-log?level=ERROR").json()["rows"]

    assert rows[0]["payload_json"] == SCREENSHOT_PAYLOAD


def test_asking_for_an_image_that_is_not_there_is_a_404_not_a_500(client) -> None:  # type: ignore[no-untyped-def]
    """认不出的 id、以及没带图的那几条，都只能是 404。

    500 会把整页控制台打成「坏了」，而这一路正是出事时要点的那个链接。
    """
    plain = client.get("/api/system-log?level=WARNING").json()["rows"][0]["id"]

    assert client.get(f"/system-log/{plain}/image").status_code == 404
    assert client.get("/system-log/999999/image").status_code == 404
    assert client.get("/system-log/999999/payload").status_code == 404
    # 没带图那一条的 payload 本身照常取得到。
    assert client.get(f"/system-log/{plain}/payload").status_code == 200


def test_the_dispatch_log_page_is_untouched(client) -> None:  # type: ignore[no-untyped-def]
    """`/logs` 仍然是派遣日志。占用它会让「哪一页看得到 runner 报错」永远说不清。

    ⚠️ 2026-09-12 从「攻击日志」改名为「派遣日志」——回收发本来就在这一页上，
    只是旧名字让人不会来这儿找它。这一条钉的是**这一页没被系统日志占用**，
    页面名只是判据；改名时跟着改名字，别把这条删了。
    """
    response = client.get("/logs")

    assert response.status_code == 200
    assert "派遣日志" in response.text
    assert "简报认不出，安全地不派" not in response.text


#: 一条**声明了 webp** 的现场图行。用真 webp 魔数（`RIFF....WEBP`），
#: 好让「返回的字节没被换过」这件事也验得出来。
WEBP_BYTES = b"RIFF\x00\x00\x00\x00WEBP" + b"vp8!" * 8_000
WEBP_PAYLOAD = json.dumps(
    {
        "note": "导航栏回读对不上出发星球",
        "thumbnail_png_base64": base64.b64encode(WEBP_BYTES).decode("ascii"),
        "thumbnail_image_format": "webp",
    },
    ensure_ascii=False,
)


def _webp_client(tmp_path):  # type: ignore[no-untyped-def]
    """只装一条 webp 现场图行的客户端。

    ⚠️ **不并进 `_seed`**：本文件里好几条用例断言「共 4 条」和「最新那一条是哪句」，
    往那批种子里加一行会把它们一起弄红，而那些断言管的是另一件事。
    """
    engine = create_database_engine(scratch_database_url(tmp_path, "webp-log.db"))
    Base.metadata.create_all(engine)
    session_factory = create_session_factory(engine)
    SystemLogRepository(session_factory).append(
        [
            SystemLogRecord(
                logged_at_utc=BASE_TIME,
                host="live-pc",
                source="tools.pirate_loop",
                level="WARNING",
                mission_kind="bot",
                message="导航栏回读对不上出发星球",
                payload_json=WEBP_PAYLOAD,
                pid=4321,
                run_id=None,
            )
        ]
    )
    app = create_persistent_app(session_factory, local_token="test-token")
    client = TestClient(app)
    client.headers.update({"X-Evo-Helper-Token": "test-token"})
    return client


def test_a_webp_screenshot_is_served_as_webp(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """⚠️ **`Content-Type` 必须按 payload 里声明的编码填，不许写死。**

    2026-09-09 起新写的缩略图是 webp。写死 `image/png` 的后果**不是显示得难看，
    是浏览器直接下载而不显示** —— 同 `battle_report_screenshots.image_format`
    单独设一列的那条理由。

    ⚠️ 这一条是**专门为了钉住接线**加的：本文件原有的那条图用例走的是
    「缺格式键（= png）」那一档，所以把 `media_type` 写回死的 `image/png`
    它照样全绿 —— 变异验证当场暴露了这个缺口。
    """
    client = _webp_client(tmp_path)
    entry_id = int(client.get("/api/system-log").json()["rows"][0]["id"])

    response = client.get(f"/system-log/{entry_id}/image")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.content == WEBP_BYTES, "字节要原样交回，不许转码"
