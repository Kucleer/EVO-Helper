"""「跑的是哪个 commit」这三样怎么取。

⚠️ **这一份最重要的一条是「取不到不许抛」**：git 不在 PATH、那个目录不是 git 仓库、
命令超时——一律降级成「不知道」并让服务照常起来。一个纯观测的功能把控制台弄得
起不来，是这里最严重的回归。

⚠️ **第二条是 `dirty` 不许说谎**：取不到时是 None（不知道），**不是 False**。
实机上有未提交改动，正是「跑的代码和 `main` 不一样」的最强信号。

`subprocess.run` 一律打桩：用例不许依赖本机环境（包括「跑测试的这个目录恰好是个
git 仓库」、「这台机器装了 git」）。只有最后那一条真的建了个临时仓库，用来验证
命令行本身写对了——打桩的用例验不了这件事，它只验逻辑。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from evo_helper.infrastructure import code_version
from evo_helper.infrastructure.code_version import CodeVersion, read_code_version


class _FakeGit:
    """按「命令里出现了什么」返回预设的输出。键是能认出那条命令的那个词。"""

    def __init__(self, replies: dict[str, tuple[int, str]]) -> None:
        self.replies = replies
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        for key, (code, out) in self.replies.items():
            if key in args:
                return subprocess.CompletedProcess(args, code, out, "")
        raise AssertionError(f"没预设这条命令：{args}")


def _install(monkeypatch: pytest.MonkeyPatch, replies: dict[str, tuple[int, str]]) -> _FakeGit:
    fake = _FakeGit(replies)
    monkeypatch.setattr(code_version.subprocess, "run", fake)
    return fake


def test_a_clean_checkout_reports_its_commit_and_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        {
            "--short": (0, "5447ca5\n"),
            "--porcelain": (0, "\n"),
            "--abbrev-ref": (0, "main\n"),
        },
    )

    version = read_code_version()

    assert version == CodeVersion(commit="5447ca5", branch="main", dirty=False)


def test_uncommitted_changes_are_reported_as_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **有未提交改动必须照实说。**

    实机上那正是「跑的代码和 `main` 不一样」的最强信号，不许因为不好看就省掉。
    """
    _install(
        monkeypatch,
        {
            "--short": (0, "5447ca5"),
            "--porcelain": (0, " M src/evo_helper/web/runtime.py\n?? scratch.py\n"),
            "--abbrev-ref": (0, "feat/something"),
        },
    )

    assert read_code_version().dirty is True


def test_a_status_that_cannot_be_read_is_unknown_not_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **`dirty=None` 是「问不出来」，不是「干净」。**

    `bool("")` 是 False，所以直接 `bool(输出)` 会把这两种情况写成同一个答案——
    而那就是让日志说假话：一台状态问不出来的机器会被记成「工作区干净」。
    """
    _install(
        monkeypatch,
        {
            "--short": (0, "5447ca5"),
            "--porcelain": (128, ""),
            "--abbrev-ref": (0, "main"),
        },
    )

    version = read_code_version()

    assert version.dirty is None
    assert version.commit == "5447ca5"


def test_a_directory_that_is_not_a_repository_degrades_to_all_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非零退出（不是 git 仓库）时三样全是「不知道」，而且不再多问两次。"""
    fake = _install(monkeypatch, {"--short": (128, "")})

    version = read_code_version()

    assert version == CodeVersion(commit=None, branch=None, dirty=None)
    assert len(fake.calls) == 1, "commit 都问不出来了，后两样白付两次超时"


@pytest.mark.parametrize(
    "boom",
    [
        FileNotFoundError("git"),
        subprocess.TimeoutExpired(cmd="git", timeout=code_version.GIT_TIMEOUT_S),
        PermissionError("git"),
    ],
)
def test_git_blowing_up_never_propagates(monkeypatch: pytest.MonkeyPatch, boom: Exception) -> None:
    """⚠️ **这一条是最严重那个回归的守门人。**

    git 不在 PATH、命令超时、没有执行权限——真实的挂机机器上这三种都会发生。
    抛出去的话，控制台**起不来**，而它本来只是想记一行日志。
    """

    def explode(*_: object, **__: object) -> subprocess.CompletedProcess[str]:
        raise boom

    monkeypatch.setattr(code_version.subprocess, "run", explode)

    assert read_code_version() == CodeVersion(commit=None, branch=None, dirty=None)


def test_every_command_carries_the_short_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **不许拖慢启动。** 每条命令都得带超时，否则一次 git 卡住就是启动挂死。"""
    seen: list[float | None] = []

    def capture(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.append(kwargs.get("timeout"))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(args, 0, "x", "")

    monkeypatch.setattr(code_version.subprocess, "run", capture)

    read_code_version()

    assert seen == [code_version.GIT_TIMEOUT_S] * 3


@pytest.mark.skipif(shutil.which("git") is None, reason="这台机器没有 git")
def test_a_real_repository_answers_all_three(tmp_path: Path) -> None:
    """真的建一个仓库跑一遍——打桩的那些用例验不了「命令行写对了没有」。

    ⚠️ 这里刻意**不指向本仓库**：用例不许依赖本机环境（比如「跑测试的这个目录
    恰好是个 git 仓库、而且刚好是干净的」）。
    """
    subprocess.run(["git", "init", "-b", "trunk", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    for args in (
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["add", "a.txt"],
        ["commit", "-m", "first"],
    ):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    clean = read_code_version(root=tmp_path)
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    dirty = read_code_version(root=tmp_path)

    assert clean.commit is not None and len(clean.commit) >= 7
    assert clean.branch == "trunk"
    assert (clean.dirty, dirty.dirty) == (False, True)


def test_the_description_says_it_cannot_tell_when_it_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写进日志正文那句话，也得把「不知道」说成不知道。"""
    _install(monkeypatch, {"--short": (128, "")})

    assert "取不到" in read_code_version().describe()
