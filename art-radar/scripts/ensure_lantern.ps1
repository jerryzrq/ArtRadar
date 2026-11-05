$ErrorActionPreference = "SilentlyContinue"

function Test-Proxy {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $client.Connect("127.0.0.1", 22307)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

if (Test-Proxy) { exit 0 }

$exe = "D:\LanternVPN\Xunlian\landeng.exe"
$work = "D:\LanternVPN\Xunlian"
$proc = Get-Process landeng -ErrorAction SilentlyContinue
if (-not $proc) {
    if (-not (Test-Path $exe)) { exit 1 }
    Start-Process $exe -WorkingDirectory $work
    Start-Sleep -Seconds 10
    $proc = Get-Process landeng -ErrorAction SilentlyContinue
}
if (-not $proc) { exit 1 }

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class LanternWin {
    [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr c);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, UIntPtr e);
    public struct RECT { public int L, T, R, B; }
}
"@

[void][LanternWin]::SetThreadDpiAwarenessContext([IntPtr](-4))
$h = ($proc | Select-Object -First 1).MainWindowHandle
if ($h -eq 0) { Start-Sleep -Seconds 3; $h = (Get-Process landeng | Select-Object -First 1).MainWindowHandle }
if ($h -eq 0) { exit 1 }

[void][LanternWin]::ShowWindow($h, 9)
Start-Sleep -Seconds 2
[void][LanternWin]::SetForegroundWindow($h)
Start-Sleep -Milliseconds 500

$rect = New-Object LanternWin+RECT
[void][LanternWin]::GetWindowRect($h, [ref]$rect)
if ($rect.L -le -30000) { exit 1 }
$w = $rect.R - $rect.L
$ht = $rect.B - $rect.T
$x = [int]($rect.L + $w * 0.5)
$y = [int]($rect.T + $ht * 0.82)

for ($i = 0; $i -lt 3; $i++) {
    [void][LanternWin]::SetCursorPos($x, $y)
    [LanternWin]::mouse_event(0x0002, 0, 0, 0, [UIntPtr]::Zero)
    [LanternWin]::mouse_event(0x0004, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 900
}

for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    if (Test-Proxy) { exit 0 }
}
exit 1
