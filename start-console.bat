@echo off
chcp 936 >nul
setlocal

rem ============================================================
rem  EVO-Helper 控制台（网页调度台）
rem
rem  双击即可启动。关掉这个黑窗口 = 停掉服务。
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

rem 先看 8770 有没有被占。曾经出现过两个陈旧服务各占一个端口、浏览器
rem 连着旧的那个，于是「代码明明改好了页面却还是坏的」查了很久。
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

rem 优先用 uv（项目本来就是这么跑的）；没有 uv 就退回 venv 里的解释器。
where uv >nul 2>nul
if errorlevel 1 goto use_venv

echo   解释器: uv run evo-web
echo   启动中。下面会打印本机与局域网地址。按 Ctrl+C 停止。
echo.
uv run evo-web
goto ended

:use_venv
if not exist ".venv\Scripts\python.exe" (
    echo   [停止] 既找不到 uv，也找不到 .venv\Scripts\python.exe。
    echo   先装依赖：uv sync
    echo.
    pause
    exit /b 1
)
echo   解释器: .venv\Scripts\python.exe
echo   启动中。下面会打印本机与局域网地址。按 Ctrl+C 停止。
echo.
.venv\Scripts\python.exe -c "from evo_helper.web.runtime import main; raise SystemExit(main())"

:ended
echo.
echo   服务已退出（退出码 %errorlevel%）。
echo.
pause
