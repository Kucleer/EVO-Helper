@echo off
chcp 936 >nul
setlocal

rem ============================================================
rem  EVO-Helper 控制台（网页调度台 + 桌面悬浮窗）
rem
rem  双击即可启动。关掉这个黑窗口 = 停掉网页服务。
rem  悬浮窗是独立进程，双击它自己关，或右键停调度器。
rem
rem  注意：本文件必须存成 GBK。cmd 用系统 OEM 代码页解析批处理，
rem  存成 UTF-8 的话中文注释会被当成命令去执行，满屏报错。
rem ============================================================

rem 切到本文件所在目录。这一行不能省：数据库路径 var\evo-helper.db 是
rem 相对当前目录解析的，而双击运行时 cmd 的当前目录未必是项目目录。
rem 不切的话会在别的地方悄悄建一个空库——页面能打开，但扫描记录、
rem 战报全都不见了，而且不报任何错。
cd /d "%~dp0"

echo.
echo   EVO-Helper 控制台
echo   目录: %CD%
echo.

rem ---- 网页服务：端口被占就停下 ----------------------------------
rem 曾经出现过两个陈旧服务各占一个端口、浏览器连着旧的那个，于是
rem 「代码明明改好了页面却还是坏的」查了很久。
set OCCUPIED_PID=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8770 .*LISTENING" 2^>nul') do set OCCUPIED_PID=%%p

if defined OCCUPIED_PID (
    echo   [停止] 8770 端口已被进程 %OCCUPIED_PID% 占用。
    echo.
    echo   多半是上一次没关干净。要停掉它，在另一个窗口执行：
    echo       taskkill /PID %OCCUPIED_PID% /F
    echo.
    echo   不要在旧服务还开着的时候再起一个：浏览器连到哪个说不准，
    echo   而旧进程跑的是旧代码，你会看到早就修好的 bug。
    echo.
    pause
    exit /b 1
)

rem ---- 悬浮窗：先清掉已有的，保证只剩一个 ------------------------
rem 全局快捷键是独占的。第二个悬浮窗注册不上 Alt+F8，只能用右键，
rem 而快捷键仍然被前一个占着——于是你按 Alt+F8 时，响应的是那个看
rem 起来「已经被换掉」的旧进程。实测踩过。
rem
rem 匹配必须同时限定进程名和完整模块路径：只按 "scan_console" 匹配
rem 会把正在执行这条命令的 shell 自己也算进去（它的命令行里就有这
rem 几个字），一条 kill 命令自杀。
echo   清理已有的悬浮窗（若有）...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*evo_helper.tools.scan_console*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" >nul 2>nul

rem 停止键用 Ctrl+Alt+F9：裸 Alt+F9 在这台机器上被 NVIDIA Overlay 占了
rem （录像开关），注册会失败。
if exist ".venv\Scripts\python.exe" (
    echo   启动悬浮窗...
    start "EVO 状态窗" /min ".venv\Scripts\python.exe" -m evo_helper.tools.scan_console --stop-key ctrl+alt+f9
)

rem ---- 网页服务：前台运行 ----------------------------------------
rem !! 用 venv 的解释器，不要用 uv run。
rem
rem uv run 每次都会先重新同步 venv（把项目重装一遍）。而上面刚起的悬浮窗用的
rem 正是 venv 里那个解释器，它把 venv 里的 .pyd 占着——于是同步时删不掉
rem greenlet 的 _greenlet.cp312-win_amd64.pyd，报「拒绝访问 (os error 5)」，
rem 网页服务直接退出码 2。实机踩过：这个 bat 自己把自己锁死了。
rem
rem 直接用 venv 解释器则什么都不改动，也省掉每次启动的同步开销。
rem venv 不存在时才退回 uv——那多半是刚 clone 完还没 uv sync。
rem
rem 另：本文件不能出现 GBK 编不出来的字符。cmd 按 OEM 代码页解析，那种字符
rem 会让整行变成乱码命令。同一个坑今天在 runtime.py、say() 和这里各踩过一次。
if not exist ".venv\Scripts\python.exe" goto use_uv

echo   解释器: .venv\Scripts\python.exe
echo   启动中。下面会打印本机与局域网地址。按 Ctrl+C 停止。
echo.
.venv\Scripts\python.exe -c "from evo_helper.web.runtime import main; raise SystemExit(main())"
goto ended

:use_uv
where uv >nul 2>nul
if errorlevel 1 (
    echo   [停止] 既没有 .venv 里的解释器，也找不到 uv。
    echo   先装依赖：uv sync
    echo.
    pause
    exit /b 1
)
echo   解释器: uv run evo-web （没有 venv，退回这条）
echo   启动中。按 Ctrl+C 停止。
echo.
uv run evo-web

:ended
echo.
echo   网页服务已退出（退出码 %errorlevel%）。悬浮窗仍在，双击它可关闭。
echo.
pause
