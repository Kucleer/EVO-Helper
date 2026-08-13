"""点「开始」那一刻的任务配置，固化成一条记录。

调度器每个 tick 都重新去库里读三条链路的配置，所以运行中改一个参数会**立刻**
生效到下一轮——而上一轮正拿着旧参数在飞。一轮之内两套口径，事后从
`mission_runs` 里只看得到一行命令行，看不出「当时页面上填的是哪一套、什么时候
换的」。运行中禁止修改（`web.persistent_service.MissionConsoleService.patch_mission`）
堵住了口子，这个模块负责留下账：**每按一次「开始」，抄一份当时的配置。**

两件事：把三条链路的配置抄成不可变的值（`FrozenTask` / `MissionConfigFreeze`），
以及把它按行追加进一个 JSONL 文件（`MissionFreezeLog`）。

**读在内存、写才碰磁盘。** 页面每 2 秒问一次调度器状态，没有理由每次都去读一遍
文件；而「开始」是用户手点的，一天不过几次。

这一层刻意不认识 `label`、「半径」这些字：它记的是**事实**（原样的
`params_json`），怎么念给人听是显示层的事（`web.persistent_service`）。
原样存 `params_json` 而不是解析后的字典，是因为它就是调度器待会儿要交给
`_command_for` 的那个字符串——解析一遍再存，存下来的就成了「我们以为的配置」。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from evo_helper.domain.scheduler import MissionKind

#: 固化记录的落脚处。与 `mission_runs` 的日志同一个 `var/` 目录。
#:
#: 选文件而不是数据库表，是因为这份东西只被追加、只被按时间倒序读，没有任何
#: 查询要跑；而 JSONL 有一个数据库给不了的性质：**用记事本就能打开**。出事的
#: 时候，用户要能不开控制台就看到「昨晚那一轮用的是哪套参数」。
DEFAULT_FREEZE_LOG = Path("var/mission-config-freezes.jsonl")

#: 内存里留多少条。页面上的历史表只看最近若干次，文件那份仍然是全的。
MAX_REMEMBERED = 200


@dataclass(frozen=True)
class FrozenTask:
    """一条链路在某一刻的配置。**只含用户改得动的那几样。**

    `disabled_reason`、`consecutive_failures` 这些是调度器自己的状态，不是配置：
    把它们记进来，一次「连崩三次被自动停用」就会让下一条记录看起来像是用户
    改了什么。
    """

    kind: MissionKind
    enabled: bool
    priority: int
    #: 原样的 `mission_tasks.params_json`，不解析。见模块头。
    params_json: str


@dataclass(frozen=True)
class MissionConfigFreeze:
    """一次「开始」固化下来的全部配置。"""

    frozen_at_utc: datetime
    tasks: tuple[FrozenTask, ...]

    def task(self, kind: MissionKind) -> FrozenTask | None:
        """这条链路当时的配置。库里缺行时为 None，调用方自己决定怎么说。"""
        return next((task for task in self.tasks if task.kind is kind), None)

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "frozen_at_utc": self.frozen_at_utc.isoformat(),
            "tasks": [
                {
                    "kind": task.kind.value,
                    "enabled": task.enabled,
                    "priority": task.priority,
                    "params_json": task.params_json,
                }
                for task in self.tasks
            ],
        }
        return json.dumps(payload, ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> MissionConfigFreeze | None:
        """一行 JSONL → 一条记录。读不懂就返回 None。

        读不懂的那一行一律跳过而不是抛：这个文件是给人看的，也就意味着它会被人
        编辑。一行手改坏了的记录不该让整台控制台起不来——而丢掉的那一行，页面上
        本来也只是历史表里的一格。

        ⚠️ **不认识的键一律无视，绝不因此丢掉整行。** 生产的那份 JSONL 里已经写进
        过 `tier_thresholds`（PR #105 加的字段，分档删掉之后不再写），而这份记录
        的用意就是「事后知道当时用的哪套参数」——为了几个多余的键把历史行读成
        `None`，等于把账毁掉。这里逐个 `data.get(...)` 取要的字段，天然满足这一条；
        改成「先校验键集合」之类的写法就会破坏它。
        """
        try:
            data: Any = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict):
            return None
        moment = _moment(data.get("frozen_at_utc"))
        if moment is None:
            return None
        raw_tasks = data.get("tasks")
        if not isinstance(raw_tasks, list):
            return None
        tasks = [task for item in raw_tasks if (task := _task(item)) is not None]
        return cls(frozen_at_utc=moment, tasks=tuple(tasks))


def freeze_now(
    tasks: Sequence[FrozenTask],
    *,
    frozen_at_utc: datetime,
) -> MissionConfigFreeze:
    """把当下这几条链路的配置封成一条记录。

    次序按 `MissionKind` 的声明顺序钉死，不跟着 `priority` 走：记录是拿来**逐条
    对比**的（这一次和上一次差在哪），两条记录里同一条链路必须落在同一格。
    优先级本身也是配置的一部分，它变了要显示成「优先级 3 → 1」，而不是让整张
    表错位、看着像三条链路全改了。
    """
    if frozen_at_utc.tzinfo is None or frozen_at_utc.utcoffset() is None:
        raise ValueError("固化时刻必须带时区，否则事后对不上是哪一分钟改的")
    order = {kind: index for index, kind in enumerate(MissionKind)}
    return MissionConfigFreeze(
        frozen_at_utc=frozen_at_utc,
        tasks=tuple(sorted(tasks, key=lambda task: order[task.kind])),
    )


class MissionFreezeLog:
    """固化记录的账本：内存里一份，磁盘上一份（可选）。

    `path` 为 None 时只留在内存里——测试与假服务那条路上不该往仓库里写文件。
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        # 构造时读一次就不再读了。之后的每一次追加都同时写内存和文件，两边不会
        # 走散；而页面每 2 秒一次的轮询只碰内存这一份。
        self._records: list[MissionConfigFreeze] = self._load()

    @property
    def path(self) -> Path | None:
        return self._path

    def records(self) -> tuple[MissionConfigFreeze, ...]:
        """全部记录，**旧的在前**。要倒序显示的是页面，不是账本。"""
        # `tuple(list)` 与 `list.append` 在 CPython 下互不撕裂，所以这里不上锁：
        # 上锁的代价是把「开始」和每 2 秒一次的状态轮询又串到一把锁上，而那正是
        # 上一轮修复刚拆开的东西。
        return tuple(self._records)

    def latest(self) -> MissionConfigFreeze | None:
        records = self.records()
        return records[-1] if records else None

    def append(self, freeze: MissionConfigFreeze) -> None:
        """记一条。**写不进文件也不能让「开始」失败。**

        磁盘满了、目录被占了、文件被别的程序锁着——这些都不该让用户点不动
        「开始」。账丢一条是遗憾，调度器起不来是事故。
        """
        self._records.append(freeze)
        del self._records[:-MAX_REMEMBERED]
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(f"{freeze.to_json()}\n")
        except OSError:
            return

    def _load(self) -> list[MissionConfigFreeze]:
        if self._path is None:
            return []
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            # 还没有这个文件（第一次跑），或者读不动。两种都当成「没有历史」。
            return []
        records = [
            record
            for line in text.splitlines()
            if line.strip() and (record := MissionConfigFreeze.from_json(line)) is not None
        ]
        return records[-MAX_REMEMBERED:]


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    # 不带时区的时刻一律丢弃，不去猜它是哪个时区：猜错八小时，事后对时间线时
    # 会把「上一轮」和「这一轮」的配置认反。
    return moment if moment.tzinfo is not None and moment.utcoffset() is not None else None


def _task(item: Any) -> FrozenTask | None:
    if not isinstance(item, dict):
        return None
    try:
        kind = MissionKind(item.get("kind"))
    except ValueError:
        return None
    enabled = item.get("enabled")
    priority = item.get("priority")
    params_json = item.get("params_json")
    if not isinstance(enabled, bool):
        return None
    # `bool` 是 `int` 的子类，得单独排掉，否则 `"priority": true` 会变成优先级 1。
    if not isinstance(priority, int) or isinstance(priority, bool):
        return None
    if not isinstance(params_json, str):
        return None
    return FrozenTask(kind=kind, enabled=enabled, priority=priority, params_json=params_json)


__all__ = [
    "DEFAULT_FREEZE_LOG",
    "MAX_REMEMBERED",
    "FrozenTask",
    "MissionConfigFreeze",
    "MissionFreezeLog",
    "freeze_now",
]
