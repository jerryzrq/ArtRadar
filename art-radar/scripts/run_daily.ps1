# 每日采集：07:30 由 Windows 任务计划调用
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$Py = "C:\Users\pc\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$LogDir = "D:\Paintings\素材\.artradar\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Log = Join-Path $LogDir ("collect-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Write-Log($msg) {
    "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $msg | Out-File -FilePath $Log -Append -Encoding utf8
}

# 蓝灯代理必须在线；先尝试自动唤起并点「连接」
& (Join-Path $PSScriptRoot "ensure_lantern.ps1") | Out-Null

$proxyOk = $false
try {
    $client = New-Object System.Net.Sockets.TcpClient
    $client.Connect("127.0.0.1", 22307)
    $proxyOk = $client.Connected
    $client.Close()
} catch { $proxyOk = $false }

if (-not $proxyOk) {
    Write-Log "代理 127.0.0.1:22307 不在线，尝试启动蓝灯…"
    if (Test-Path "D:\LanternVPN\Xunlian\landeng.exe") {
        Start-Process "D:\LanternVPN\Xunlian\landeng.exe" -WorkingDirectory "D:\LanternVPN\Xunlian"
        Start-Sleep -Seconds 12
        try {
            $client = New-Object System.Net.Sockets.TcpClient
            $client.Connect("127.0.0.1", 22307)
            $proxyOk = $client.Connected
            $client.Close()
        } catch { $proxyOk = $false }
    }
}

if (-not $proxyOk) {
    Write-Log "代理仍不可用，本次采集取消。请打开蓝灯并点击连接。"
    exit 1
}

Write-Log "开始采集"
& $Py (Join-Path $Root "artradar.py") collect *>> $Log
Write-Log "开始生成今日精选"
& $Py (Join-Path $Root "artradar.py") pick --n 12 *>> $Log
Write-Log "完成"
