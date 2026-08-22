"""跑着的到底是哪个 commit。**取不到就说取不到，绝不让服务起不来。**

## ⚠️ 这个缺口是什么

实机跑在另一台机器上（用户口径 2026-08-20），而从库里能查到的只有：

- `alembic_version`——**库**升到了哪个 revision；
- `system_log` 的 `host` / `pid`——哪台机器、进程什么时候换的。

**这两样推不出「代码停在哪个 commit」。** 于是有一个查不出来的失效场景：那台机器
**没 pull**、还停在旧 commit，重启 bat 只会把库升到**旧 commit 所知的 head**，
而我们从库里看不出来，会误以为已经升到 `main` 的 head。

所以启动时把 commit / 分支 / 工作区是否干净写进 `system_log`。

## ⚠️ 三条硬要求

1. **取不到就降级。** git 不在 PATH、那个目录不是 git 仓库、命令超时——一律记成
   「取不到」并照常启动。这个仓的惯例是可选依赖缺失时降级运行而不是报错
   （`plyer` / `pytesseract` / `pygetwindow` 都是这么写的），跟着走。
   ⚠️ 一个纯观测的功能把控制台弄得起不来，是这里最严重的回归，有用例钉着。
2. **不许拖慢启动。** 每条命令一个 `GIT_TIMEOUT_S` 的短超时。
3. **只记这三样。** 仓库是公开的，日志会被贴出来看——本地路径、用户名、远端地址
   一概不进 payload。

## ⚠️ `dirty` 要照实说，而且「不知道」≠「干净」

实机上如果有未提交改动，那正是「跑的代码和 `main` 不一样」的**最强信号**，
不许因为不好看就省掉。取不到时是 `None`（不知道），**不是 `False`**：
把「问不出来」写成「干净的」是让日志说假话，而这一整个模块存在的理由正是
不让日志说假话。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

#: 每条 git 命令的超时（秒）。**标定常量，不是旋钮**：它的取值由「不许拖慢启动」
#: 这一条决定，不是用户偏好。三条命令最坏加起来 6 秒，而正常情况下是毫秒级。
GIT_TIMEOUT_S = 2.0


@dataclass(frozen=True, slots=True)
class CodeVersion:
    """这个进程跑的是哪份代码。**每一项都可能是「不知道」（None）。**"""

    #: 短 sha。None = 取不到（git 不在 PATH、不是 git 仓库、超时）。
    commit: str | None
    #: 分支名。detached HEAD 时 git 给的是 `HEAD`，照实存。
    branch: str | None
    #: 工作区有没有未提交改动。⚠️ **None 是「不知道」，不是「干净」。**
    dirty: bool | None

    @property
    def known(self) -> bool:
        return self.commit is not None

    def describe(self) -> str:
        """写进日志正文那一句人话。"""
        if not self.known:
            return "代码版本取不到（git 不可用或这不是一个 git 仓库）"
        state = {True: "有未提交改动", False: "工作区干净", None: "改动状态取不到"}[self.dirty]
        return f"{self.branch or '分支未知'} @ {self.commit}（{state}）"


def read_code_version(*, root: Path | None = None) -> CodeVersion:
    """问 git 要三样。**任何一样问不出来就是 None，绝不抛异常。**

    `root` 默认取仓库根（这个文件往上四层）。给它一个参数是为了让用例能指向一个
    临时仓库——用例不许依赖本机环境，包括「跑测试的那个目录恰好是个 git 仓库」。
    """
    base = root or Path(__file__).resolve().parents[3]
    commit = _git(base, "rev-parse", "--short", "HEAD")
    if commit is None:
        # 连 commit 都问不出来时别再问后两样：同样会失败，白付两次超时。
        return CodeVersion(commit=None, branch=None, dirty=None)
    porcelain = _git(base, "status", "--porcelain")
    return CodeVersion(
        commit=commit,
        branch=_git(base, "rev-parse", "--abbrev-ref", "HEAD"),
        # ⚠️ 空字符串（干净）和 None（问不出来）必须分开：`bool("")` 是 False，
        # 直接 `bool(porcelain)` 会把「不知道」写成「干净」。
        dirty=None if porcelain is None else bool(porcelain.strip()),
    )


def _git(root: Path, *args: str) -> str | None:
    """跑一条 git 命令，输出去掉首尾空白。**失败一律 None。**

    捕的是 `OSError`（git 不在 PATH）、`subprocess.SubprocessError`（超时等）与
    非零退出码（不是 git 仓库）。这三种在实机上都真的会发生，而任何一种都不该
    让控制台起不来。
    """
    try:
        completed = subprocess.run(  # noqa: S603 - 参数全是本模块里写死的字面量
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


__all__ = ["GIT_TIMEOUT_S", "CodeVersion", "read_code_version"]
