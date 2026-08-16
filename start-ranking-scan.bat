@echo off
chcp 936 >nul
setlocal

rem ============================================================
rem  EVO-Helper 军力榜采集（通宵版）
rem
rem  这个进程会真的拖鼠标。它和调度器起的任务 runner（海盗/bot/扫描）
rem  共用同一只鼠标，而 bot 那条 allow_actions 是开着的——两个一起拖，
rem  攻击链路的回读会落在被本进程拖走的画面上。
rem
rem  所以每一趟开始前都先让路：有 runner 活着就等，等它跑完再扫。
rem  这就是「攻击发出后的间歇时间拿来扫描」。
rem
rem  注意：本文件必须存成 GBK。cmd 按系统 OEM 代码页解析，存成 UTF-8 的话
rem  中文注释会被当成命令去执行，报一堆错（start-console.bat 里记着这条）。
rem ============================================================

cd /d "%~dp0"

echo.
echo   EVO-Helper 军力榜采集
echo   目录: %CD%
echo.

if not exist ".venv\Scripts\python.exe" (
    echo   [停止] 找不到 .venv\Scripts\python.exe
    echo   先跑一遍: uv sync
    echo.
    pause
    exit /b 1
)

echo   开始通宵采集。停止请按 Ctrl+C（急停也可以：鼠标甩到屏幕左上角）。
echo.

:loop
call :yield
echo.
echo   ==== 新的一趟 %date% %time% ====
.venv\Scripts\python.exe -m evo_helper.tools.ranking_scan
set RC=%errorlevel%
echo   本趟退出码 %RC%   0=正常到底  1=开榜失败  2=中途离页/断线
if "%RC%"=="1" (
    rem 开榜失败多半是画面不在游戏里（掉线回到入口页）。歇久一点再试，
    rem 别把「认不出画面」变成每分钟重试一次的空转。
    timeout /t 120 /nobreak >nul
) else (
    rem 两趟之间歇一会儿：给 Ctrl+C 留窗口，也避免连续开关面板。
    rem 每趟都从榜首重新翻——关掉面板再打开列表回顶部（用户实测），
    rem 所以每趟都要重付约 6 分钟的「翻真人段」税。已知代价，不是 bug。
    timeout /t 60 /nobreak >nul
)
goto loop

rem ---- 让路 ----------------------------------------------------
rem 轮询而不是加锁，存在竞态：本进程刚查完「没人」、正要开始拖，调度器恰好
rem 起了一个 runner——那一瞬间两边都会动鼠标。窗口只有「查完到开拖」之间，
rem 而 bot 链路每一步都有回读兜底，最坏是那个目标这一趟作废，不会误派舰队。
rem 要根治得让调度器和本进程共用一把文件锁，那是「把军力榜做成一种
rem MissionKind」时一并解决的事。
:yield
for %%M in (pirate_loop bot_loop scan_coordinates) do (
    powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*evo_helper.tools.%%M*' }) { exit 1 } else { exit 0 }"
    if errorlevel 1 (
        echo   [让路] evo_helper.tools.%%M 正在跑，等 60 秒再看...
        timeout /t 60 /nobreak >nul
        goto yield
    )
)
goto :eof
