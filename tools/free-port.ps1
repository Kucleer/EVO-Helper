# 腾空一个端口：找到监听它的进程，连同整棵进程树杀掉，确认真的空了。
#
# 为什么不写在 start-console.bat 里：那一段试过用 `^` 续行的多行 PowerShell，
# cmd 把带引号和 `$` 的行拼坏了，命令根本没执行——表现是「清理过了但端口还被
# 同一个 PID 占着」。批处理续行 + PowerShell 引号是已知的雷区，独立文件没有这个问题。
#
# 为什么按端口找而不是按命令行匹配进程：uvicorn 会自己再开一个子进程。按命令行
# 匹配杀掉父进程之后，子进程接管端口继续服务，端口一秒都没空出来——实测杀掉
# 30464、43724 之后 8770 立刻由 42204 接着监听。孤儿子进程只能从端口反查。
#
# 退出码 0 = 端口已空（本来就空，或已成功腾出）；1 = 试满仍被占。

param(
    [int]$Port = 8770,
    [int]$Attempts = 5
)

function Get-Listeners {
    param([int]$Port)
    @(
        Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

for ($i = 0; $i -lt $Attempts; $i++) {
    $owners = Get-Listeners -Port $Port
    if (-not $owners) {
        if ($i -gt 0) { Write-Host "  端口 $Port 已腾空" }
        exit 0
    }
    foreach ($pid_ in $owners) {
        Write-Host "  停止进程树 $pid_（占用 $Port）"
        # /T 连子进程一起杀。少了它，uvicorn 的子进程会接管端口继续跑。
        & taskkill.exe /PID $pid_ /T /F 2>&1 | Out-Null
    }
    Start-Sleep -Milliseconds 800
}

if (Get-Listeners -Port $Port) { exit 1 }
exit 0
