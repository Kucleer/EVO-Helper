@echo off
chcp 936 >nul
setlocal

rem ============================================================
rem  EVO-Helper 控制台（网页调度台）
rem
rem  双击即可启动。关掉这个黑窗口 = 停掉网页服务。
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

rem ---- 网页服务：先腾空 8770，再起新的 ------------------------
rem 原来是「发现端口被占就停下、让人手动 kill」，实机上没挡住：同一天两次
rem 出现两代服务并存，旧的占着 8770 跑几小时前的代码，浏览器连的一直是它，
rem 表现成「代码明明合并了页面还是旧的」。
rem
rem 腾空的逻辑放在 tools 目录下的 free-port.ps1 里，那里写清了两件事：
rem 为什么必须按端口反查而不是按命令行匹配（uvicorn 的子进程会接管端口），
rem 以及为什么不能把多行 PowerShell 用 ^ 续行写在 bat 里（会被 cmd 拼坏）。
echo   Cleaning up any service holding port 8770 ...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools/free-port.ps1" -Port 8770
if errorlevel 1 (
    echo.
    echo   [STOP] Port 8770 is still in use and could not be freed.
    echo   Check who owns it:  netstat -ano ^| findstr :8770
    echo.
    pause
    exit /b 1
)

rem ---- 网页服务：前台运行 ----------------------------------------
rem !! 用 venv 的解释器，不要用 uv run。
rem
rem uv run 每次都会先重新同步 venv（把项目重装一遍）。只要有别的进程占着 venv
rem 里的 .pyd，同步就删不掉 greenlet 的 _greenlet.cp312-win_amd64.pyd，报
rem 「拒绝访问 (os error 5)」，网页服务直接退出码 2。实机踩过：当时占用者是
rem 这个 bat 自己刚起的那个桌面悬浮窗（已删），它自己把自己锁死了。
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
echo   网页服务已退出（退出码 %errorlevel%）。
echo.
pause
