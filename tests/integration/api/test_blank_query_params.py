"""查询参数是空串时不许 422——那是页面自带的筛选项走出来的路径。

星球列表页的银河系下拉框里，「全部银河系」那一项的 `value` 就是空串：

    <option value="">全部银河系</option>

浏览器提交表单时必然带上 `galaxy=`。而参数原先声明成 `int | None`，FastAPI 拿空串
去解析整数直接 422，返回的还是一页 JSON 报错，不是页面：

    {"detail":[{"type":"int_parsing","loc":["query","galaxy"],
                "msg":"Input should be a valid integer, unable to parse string
                       as an integer","input":""}]}

也就是说**这一页默认的那个筛选项点下去就报错**。翻页链接自己不会带空串
（`page_url` 只在 `galaxy is not None` 时才拼它），所以这个坑只有走表单才踩得到——
而那恰恰是用户会走的路。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from evo_helper.web.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_the_all_galaxies_option_does_not_blow_up(client: TestClient) -> None:
    """用户实际点出来的那个 URL。"""
    assert client.get("/planets?galaxy=&kind=bot&limit=200").status_code == 200


def test_a_real_galaxy_still_filters(client: TestClient) -> None:
    """兼容空串不能把「真的传了银河系」这条路弄坏。"""
    assert client.get("/planets?galaxy=2&kind=bot&limit=200").status_code == 200


@pytest.mark.parametrize("query", ["limit=", "offset=", "limit=&offset=", "galaxy=&limit=&offset="])
def test_other_blank_numeric_params_fall_back_to_defaults(client: TestClient, query: str) -> None:
    """分享出去的链接被人手改成 `?limit=` 是同一类 422。

    这几个现在是下拉框、不会提交空串，但「修一处漏一处」的成本比统一处理高。
    """
    assert client.get(f"/planets?{query}").status_code == 200


def test_a_bad_galaxy_is_still_rejected(client: TestClient) -> None:
    """放行的只有空串。`galaxy=abc` 仍该 422——否则就成了「什么都收」。"""
    assert client.get("/planets?galaxy=abc").status_code == 422
