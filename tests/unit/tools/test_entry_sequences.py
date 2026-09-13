"""每个**新入口**都要抄全 `run()` 的开工序列。

⚠️⚠️ 这一条是 2026-09-13 一天之内被实机打回来**两次**打出来的：

| 入口 | 漏了哪一步 | 症状 |
|---|---|---|
| `attack_stargate`（星门） | `_ensure_session` | 游戏停在登录页，整趟从登录页上开始点 |
| `run_mail_only`（空闲回读） | `_require_system_view` | 信箱翻完了，收尾切不回视图，白跑 |

两次都是「抄了一部分」。`run()` 的开工是**四步**，缺任何一步的失败都发生在**后面很远的地方**，
从症状反推不到这里 —— 所以用例直接钉序列本身。

⚠️ 这里钉的是「调了没有」，不是「怎么调」。四步各自的判据在它们自己的用例里；
这一条只防「新入口少抄一步」。
"""

from __future__ import annotations

import inspect

import pytest

from evo_helper.tools.pirate_loop import PirateLoop

#: `run()` 开工那四步。**顺序有意义**：会话没接回来时画面认不出，
#: 认不出时切视图只会朝视图菜单坐标盲点三次然后放弃（`_ensure_session` 的 docstring）。
OPENING_STEPS = (
    "ensure_game_window",
    "_ensure_session",
    "_reset_to_known_screen",
    "_require_system_view",
)

#: 自己起一趟、不经过 `run()` 的入口。**新增入口要加进这张表。**
STANDALONE_ENTRIES = ("run_mail_only",)


@pytest.mark.parametrize("entry", STANDALONE_ENTRIES)
@pytest.mark.parametrize("step", OPENING_STEPS)
def test_a_standalone_entry_does_the_whole_opening(entry: str, step: str) -> None:
    source = inspect.getsource(getattr(PirateLoop, entry))

    assert step in source, (
        f"{entry} 少了开工的 {step}。四步缺任何一步，失败都发生在后面很远的地方 ——"
        "2026-09-13 星门漏 _ensure_session、空闲回读漏 _require_system_view，各白跑一趟。"
    )


def test_the_opening_steps_are_what_run_actually_does() -> None:
    """⚠️ **反过来也要钉住**：上面那张表必须跟着 `run()` 走。

    `run()` 哪天多一步而这张表没跟上，上面那几条就会变成「守着一份过期的清单」——
    全绿，却漏掉新增的那一步。
    """
    source = inspect.getsource(PirateLoop.run)

    for step in OPENING_STEPS:
        assert step in source, f"`run()` 里已经没有 {step} 了；这张表过期了，请更新"
