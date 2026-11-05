param(
    [string]$Time = "07:30",
    [string]$TaskName = "ArtRadar-Collect"
)

$ErrorActionPreference = "Stop"
$Runner = Join-Path $PSScriptRoot "run_daily.ps1"
if (-not (Test-Path $Runner)) { throw "找不到 $Runner" }

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$Runner`"" `
    -WorkingDirectory (Split-Path $PSScriptRoot -Parent)
$trigger = New-ScheduledTaskTrigger -Daily -At $Time
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "art-radar 每日绘画参考采集" -Force | Out-Null

Write-Host "已注册计划任务 '$TaskName'，每天 $Time 运行。"
Write-Host "手动测试: Start-ScheduledTask -TaskName '$TaskName'"
Write-Host "查看日志: D:\Paintings\素材\.artradar\logs"
