"""补录日志从子进程写下去、到页面读回来，中文得还是中文。

## 这一条修的是什么

2026-08-19：「战报补录」面板下方那片日志流整片乱码——

```
16:02:02 ??%botu????UTC 2026-08-19 ????? 12 ?????? 60 ??:??%g'????????
```

中文全变成 `?`，而时间戳、`VICTORY`、日期这些 ASCII 是好的。

编码在**起子进程**那一环丢的，不在传输、也不在页面：

- 日志文件由父进程 `open(..., encoding="utf-8")` 打开，但那个 `encoding`
  **只管父进程自己写的字节**；
- 子进程拿到的是继承来的文件描述符，写什么字节由**它自己的** `sys.stdout` 决定，
  而 Windows 上重定向到文件的 `sys.stdout` 用机器的 ANSI 代码页（cp936/GBK）；
- 读的那一侧 `read_text(encoding="utf-8", errors="replace")` 把 GBK 字节全换成
  U+FFFD——GBK 的低位字节又落在 ASCII 区，于是还掺进 `u`、`'`、`S` 这类凭空冒出来
  的字母，正是面板上看到的那副样子。

实证：修复前那台机器上留下的 `var/logs/backfill-pirate.log`，按 cp936 解得出
「补录pirate战报：UTC 2026-08-12 起，…」，按 UTF-8 解全是替换符。

## 两条判据

- **写**：子进程的环境里必须钉着 `PYTHONIOENCODING=utf-8`；
- **读**：同一个文件里前后两种编码都得读对——修复落地之后，那些一直追加、
  从不轮转的 `backfill-*.log` 前半截是 GBK、后半截是 UTF-8。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evo_helper.application import mission_supervisor
from evo_helper.application.backfill import (
    BackfillCoordinator,
    BackfillRequest,
    launch_backfill,
)
from evo_helper.application.mission_supervisor import (
    child_log_environment,
    decode_log_text,
    launch_mission,
)
from evo_helper.domain.scheduler import MissionKind

NOW = datetime(2026, 8, 19, 8, 2, tzinfo=UTC)

#: 面板上那一行的原文（坐标换成假的，本仓是公开仓）。
CHINESE_LINE = "16:02:02 补录bot战报：UTC 2026-08-19 起，最多翻 12 屏、开 60 封"
#: 同一行里 ASCII 那半截——它在乱码里是好的，所以光断言「有中文」还不够，
#: 得连它一起断言，否则「整行都没读到」也能蒙混过去。
ASCII_PART = "16:02:02"


class _FakeProcess:
    pid = 4321

    def poll(self) -> int | None:
        return None

    def terminate(self) -> None:
        return None

    def wait(self, timeout: float | None = None) -> int:
        return 0


# -- 写：子进程按什么编码写 -----------------------------------------------------


def test_the_child_is_told_to_write_utf8() -> None:
    """少了这一条，Windows 上的子进程按 ANSI 代码页写日志，中文全变问号。"""
    assert child_log_environment({"PATH": "/usr/bin"})["PYTHONIOENCODING"] == "utf-8"


def test_the_rest_of_the_environment_survives() -> None:
    """只钉编码，别的一个都不动：子进程要靠环境里的库路径、凭据、任务身份跑起来。"""
    env = child_log_environment({"PATH": "/usr/bin", "EVO_HELPER_RUN_ID": "abc"})

    assert env["PATH"] == "/usr/bin"
    assert env["EVO_HELPER_RUN_ID"] == "abc"


@pytest.mark.parametrize("launcher", ["backfill", "mission"])
def test_both_launchers_pin_the_child_encoding(
    launcher: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """两个进程管理器各起各的子进程，**两边都要钉**。

    补录那一侧最要紧（页面上那份日志尾巴是它跑十几分钟里唯一的进度来源），
    但任务那一侧的 `mission-*.log` 同样是人出事之后要读的东西。

    ⚠️ 这里把 `subprocess.Popen` 换掉，**绝不真的起一个子进程**。
    """
    seen: dict[str, Any] = {}

    def fake_popen(command: Sequence[str], **kwargs: Any) -> _FakeProcess:
        seen.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(mission_supervisor.subprocess, "Popen", fake_popen)
    log_path = tmp_path / "logs" / "child.log"

    if launcher == "backfill":
        from evo_helper.application import backfill as backfill_module

        monkeypatch.setattr(backfill_module.subprocess, "Popen", fake_popen)
        launch_backfill(["python", "-c", "pass"], log_path)
    else:
        launch_mission(MissionKind.BOT, ["python", "-c", "pass"], log_path)

    assert seen["env"]["PYTHONIOENCODING"] == "utf-8"


# -- 读：同一个文件里两种编码都得读对 -------------------------------------------


def test_utf8_lines_come_back_as_chinese() -> None:
    assert decode_log_text(CHINESE_LINE.encode("utf-8"), legacy_encoding="cp936") == CHINESE_LINE


def test_legacy_gbk_lines_come_back_as_chinese() -> None:
    """修复之前写下的那些日志。不回退的话，它们会永远显示成一片问号。"""
    decoded = decode_log_text(CHINESE_LINE.encode("cp936"), legacy_encoding="cp936")

    assert decoded == CHINESE_LINE
    assert "�" not in decoded


def test_a_file_with_both_encodings_reads_right_on_both_halves() -> None:
    """日志按链路分文件、一直追加、从不轮转，所以修复之后必然出现混编文件。

    整份挑一种编码解都会有一半是乱码——**只有逐行判**才能两边都对。
    """
    old = "16:02:02 旧的一行，GBK 写的".encode("cp936")
    new = "16:03:03 新的一行，UTF-8 写的".encode()

    decoded = decode_log_text(old + b"\n" + new, legacy_encoding="cp936")

    assert decoded.splitlines() == ["16:02:02 旧的一行，GBK 写的", "16:03:03 新的一行，UTF-8 写的"]


def test_utf8_wins_when_a_line_could_be_read_either_way() -> None:
    """次序不能反：**先试 UTF-8**，失败才回退。

    GBK 收得下的字节范围比 UTF-8 宽得多，一段 UTF-8 中文常常也是一段合法 GBK
    ——只是意思全变了。先试 GBK 的话它不报错、也就永远回退不到 UTF-8，新写的
    中文会一律被解成乱码。

    这里挑的正是这样一行：`'补录完成'` 的 UTF-8 字节按 GBK 解得出 `'琛ュ綍瀹屾垚'`
    ——**不抛异常**。随手挑一句中文多半会撞上 GBK 解不了的字节，那种句子无论
    次序怎么排都能过，这条用例也就什么都不守了。
    """
    line = "16:03:22 补录完成"
    assert line.encode("utf-8").decode("cp936") != line, "挑的这一行 GBK 解不了，守不住次序"

    assert decode_log_text(line.encode("utf-8"), legacy_encoding="cp936") == line


def test_undecodable_bytes_do_not_blow_up() -> None:
    """日志读不出来不能报错：一次读文件失败把状态接口打成 500，页面连「在跑」
    都显示不出来了。
    """
    assert decode_log_text(b"\xff\xfe abc", legacy_encoding="ascii").endswith(" abc")


# -- 端到端：写进文件的中文，从状态里读出来还是中文 -----------------------------


def _coordinator(tmp_path: Path) -> BackfillCoordinator:
    coordinator = BackfillCoordinator(
        launch=lambda command, log_path: _FakeProcess(),
        clock=lambda: NOW,
        log_dir=tmp_path / "logs",
    )
    coordinator.request(BackfillRequest(kind="bot", since=NOW.date()))
    return coordinator


@pytest.mark.parametrize("written_as", ["utf-8", "cp936"])
def test_the_log_tail_hands_back_chinese(written_as: str, tmp_path: Path) -> None:
    """页面读的就是这个方法。它出来是问号，面板上就是问号。

    两种编码都过一遍：`cp936` 那一支代表修复之前留下的日志，`utf-8` 那一支
    代表修复之后的——**面板不该知道自己读的是哪一茬**。
    """
    coordinator = _coordinator(tmp_path)
    path = coordinator.state().log_path
    assert path is not None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(CHINESE_LINE.encode(written_as))

    tail = coordinator.log_tail()

    assert tail == CHINESE_LINE
    assert ASCII_PART in tail
    assert "?" not in tail
