---
issue: 269
agent: game-runner
type: Fixed
date: 2026-08-28
---

**窗口守护日志的链路名改由入口传字面量，不再从调用栈上猜。**

## 症状

#268 发布后第一条守护日志（生产，2026-08-28 14:40:12）：

    source='__main__'  mission_kind='bot'  outcome=window_already_up
    guard_version=window-restart-guard/1

`source` 本该是 `tools.bot_loop` / `tools.pirate_loop` / `tools.ranking_scan` ——
「六个任务同时倒下时，是哪一条链路」正是那个字段存在的全部理由。写成 `__main__`
之后三条链路一模一样：**键在、值没用**。

## 缺陷

第一版走 `_caller_source()`（`sys._getframe(2).f_globals["__name__"]`）。四个调用点
**都在各自模块的 `main()` 里，而那几个模块是当脚本跑的** —— `__name__` 就是
`"__main__"`。栈上取模块名这招只对「被 import 的模块里的函数」成立，对入口本身
恰恰不成立，而这里全都是入口。

## ⚠️⚠️ 为什么用例接不住 —— 测试环境与生产语义相反

不是写漏了。上一版那条用例断言的是：

    assert recorder.rows[-1][1].endswith("test_window_restart_guard")

**它在测试里恒真**：测试模块是被 import 的，`__name__` 就是模块名。也就是说，
这条用例越绿越说明不了问题 —— 它验证的前提（调用方是个被 import 的模块）在生产上
恰好不成立。

⇒ 用例改成钉「交进去什么就记什么」，与调用方叫什么名字无关；另加一条直接读源码、
断言四个入口各报各的名字且互不相同（「四个入口都填对了」没法靠跑一遍来证明，
跑起来要真窗口）。

## `chain` 必填

给默认值的话，新加一条链路时漏填就不再是 `TypeError`，而是又一条说谎的日志 ——
正是这次的症状，而且要等上了生产才看得出来。「漏填会当场炸」是这个参数唯一比自动
嗅探强的地方，所以单独钉了一条读签名的用例。

⚠️ 这与 `say` 那边的取舍不同：那边 136 个调用点，改签名要全改一遍、漏一个就说谎，
自动嗅探是对的。这里只有四个。（`say` 从入口层调用时有同样的 `__main__` 问题，
本次不动它。）

- Configuration: 无新增配置项。
- Database: 无迁移，只改 `system_log.source` 写什么。
- Verification: `ruff format --check` / `ruff check` 干净；`mypy src` 3 条既有错误未
  新增；`pytest -q`（与 CI 同一条命令）→ 4211 passed / 256 skipped。**五处变异各有
  对应用例变红**（退回从调用栈猜 / 两个入口报同名 / 入口填成 `__main__` /
  `chain` 给默认值 / `chain` 改成位置参数）。其中「`chain` 给默认值」第一轮是**绿的**
  ——「必填」本身没被钉住，补了读签名那一条才接住。
- Safety: 只改日志里的一个字段，窗口守护的分档与行为一个字没动。
- Rollback: `source = chain` 改回 `source = _caller_source()`、四个调用点去掉参数。
