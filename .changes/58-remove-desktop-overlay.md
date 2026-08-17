---
issue: 58
agent: root
type: Removed
date: 2026-08-17
---

桌面悬浮窗 `tools/scan_console.py` 整个删掉，`start-console.bat` 不再起它。

## 它原来是什么

屏幕右下角一个 200×92 的无边框状态灯，由 `start-console.bat` 起成一个**独立
进程**（不是控制台进程里的线程）。它显示三样：跑没跑、当前跑的是哪条链路、
这条链路跑了多久；外加两个全局快捷键（`Alt+F8` 起 / `Alt+F9` 停）和右键停。

它的存在理由写在自己的模块注释里：任务期间游戏窗口一直占着前台，浏览器里的控制台
被压在后面看不见，「现在跑的是哪条链路」只能靠猜。

## 为什么删

用户口径（2026-08-17）：「关闭桌面悬浮窗，因为现在可以远程控制了」。控制台绑
`0.0.0.0:8770`，工作机浏览器直接打开就行——挂机机器的前台被谁占着已经不重要了。
留着它就是白占一块屏幕、一个 tkinter 线程、一个每秒一次的轮询，外加一对独占的
全局快捷键（实机上 `Alt+F9` 还和 NVIDIA ShadowPlay 打过架）。

## 改了什么

- 删 `src/evo_helper/tools/scan_console.py`（752 行）与
  `tests/unit/tools/test_scan_console.py`（440 行）。整个模块只有它自己在用：
  `SchedulerClient` / `SchedulerPoller` / `ConsoleController` / `HotkeyListener`
  没有第二个调用方。
- `start-console.bat` 去掉两段：起悬浮窗那一段，以及它前面「先 kill 掉已有的
  悬浮窗」那一段。文件仍存 GBK。
- 新增 `tests/unit/tools/test_no_desktop_overlay.py`：钉住 bat 里不再出现那个
  模块、包里也找不到它。**删除类改动最容易被悄悄撤销的方式是把 bat 那一行加
  回去，那不碰任何 Python 代码。**
- `tests/integration/api/test_scheduler_api.py` 里那条「悬浮窗契约」改写成
  「页面顶栏契约」：断言一条不少，只是不再经过悬浮窗的解析器。`current.label`
  这个字段全仓只有那一条在断言，删掉它会静静地丢掉覆盖。
- 一批注释里指向 `tools/scan_console.py` 的路径改掉。悬在注释里的模块路径和悬在
  代码里的 import 一样会骗人。

## 共用的东西一样没动

`web/display.py` 的 `MISSION_LABELS`、`STATUS_TONES`、`STATUS_GLYPHS`，
`domain/scheduler.TaskStatus`，`web/security.default_local_token()`
（服务端自己在用，跨机器写请求要带令牌），以及 `GET /api/scheduler` 的字段——
全部原样。**页面一个像素都没变。**

`game/overlay.py` 与它无关：那是游戏里那些盖在画面上的面板的关闭键，名字撞了而已。

- Configuration: 无。悬浮窗从来没有开关（配置项 / 环境变量都没有），唯一的入口
  就是 bat 里那一行 `start`，所以只能删。
- Database: 无。
- Verification: `pytest -q` / `ruff check src tests` / `ruff format --check src tests`
  / `mypy src` 四道全绿。变异测试：把 bat 那行 `start` 加回去 →
  `test_the_startup_script_never_launches_the_overlay` 红；把模块文件恢复 →
  `test_the_overlay_module_is_gone` 红。
- Safety: 少了一条急停路径——`Ctrl+Alt+F9`。另外两条都在：鼠标甩到屏幕左上角
  （`pyautogui.FAILSAFE`）、控制台点「结束」或 `POST /api/scheduler/stop`
  （**远程也能打**，这条正是用户说的「现在可以远程控制」）。
  已在 `docs/部署到挂机机器.md` 的「紧急停止」一节里写明。
- Rollback: `git revert` 即可；代码本身在 git 历史里，不留兼容层、不留开关。
