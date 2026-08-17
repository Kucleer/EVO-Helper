"""桌面悬浮窗已经删了，启动路径里不许再把它拉起来。

## 为什么要有这一条

悬浮窗（`evo_helper.tools.scan_console`）曾经是屏幕右下角那个 200×92 的状态灯，
由 `start-console.bat` 起成一个**独立进程**。控制台能远程访问之后它就没有存在
意义了：它显示的三样（跑没跑、跑的是哪条链路、跑了多久）页面上全有，而它还占着
屏幕、占着一个 tkinter 线程和一对全局快捷键（用户口径 2026-08-17）。

删掉一个东西，最容易被悄悄撤销的方式不是有人把整个模块写回来，而是**有人把那
一行 `start` 加回 bat**——那不会碰任何 Python 代码，因此不会有任何测试变红。
所以这条查的是**启动路径本身**：bat 里不许再出现那个模块，包里也不许再有它。

`start-console.bat` 存的是 GBK（`cmd` 按系统 OEM 代码页解析批处理，见文件头部
那段注释），所以这里必须按 GBK 读，不能拿 UTF-8 去读。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

#: 仓库根。`tests/unit/tools/` 往上三层。
ROOT = Path(__file__).resolve().parents[3]

START_CONSOLE_BAT = ROOT / "start-console.bat"


def _bat_text() -> str:
    return START_CONSOLE_BAT.read_text(encoding="gbk")


def test_the_batch_file_is_still_there() -> None:
    """先证明下面那条读的是真文件——路径写错的话它会静静地永远绿。"""
    assert START_CONSOLE_BAT.is_file()
    assert "evo_helper.web.runtime" in _bat_text()


def test_the_startup_script_never_launches_the_overlay() -> None:
    """`start-console.bat` 里一个字都不许提那个模块。

    包括那条「先清掉已有的悬浮窗」的 kill：留着它等于告诉下一个人这个进程还在。
    """
    text = _bat_text()

    assert "scan_console" not in text, "start-console.bat 又在起桌面悬浮窗了"
    assert "start " not in text, "start-console.bat 现在只该前台跑网页服务，不另起进程"


def test_the_overlay_module_is_gone() -> None:
    """模块本身也不许再回来——留着不可达的死代码比删掉更糟。

    要找回它去 git 历史里翻，不在包里留一份没人起的副本。
    """
    assert importlib.util.find_spec("evo_helper.tools.scan_console") is None


def test_no_source_file_still_points_at_the_overlay() -> None:
    """`src/` 里不许再有指向那个模块的引用，包括注释里的路径。

    悬在注释里的模块路径和悬在代码里的 import 一样会骗人：下一个人照着去看，
    看到的是一个不存在的文件。
    """
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if "scan_console" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
