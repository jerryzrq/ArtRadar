$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
$DefaultPy = "C:\Users\pc\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$Py = if ($env:ART_RADAR_PYTHON) { $env:ART_RADAR_PYTHON } elseif (Test-Path $DefaultPy) { $DefaultPy } else { "python" }

Get-Process ArtRadar -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 1

Set-Location $Root
& $Py -m PyInstaller --noconfirm --clean --onefile --console --name ArtRadar `
    --paths art-radar `
    --add-data "art-radar\config.toml;." `
    --add-data "art-radar\ui.html;." `
    --add-data "art-radar\scripts;scripts" `
    --collect-all playwright `
    art-radar\app.py

# Raise integrity level so the exe can write the D: library.
$ExePath = Join-Path $Root "dist\ArtRadar.exe"
& "C:\Windows\System32\icacls.exe" $ExePath /setintegritylevel Medium | Out-Null
Write-Host "built: $ExePath"
Write-Host "run:   $ExePath"
exit 0
