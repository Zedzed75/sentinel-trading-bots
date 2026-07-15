# Etat de la flotte Sentinel : un OK/ABSENT par bot + watchdog.
# Usage : powershell -File ops\status.ps1
# Code de sortie : 0 si tout tourne, 1 sinon (utilisable en script).

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "logs"
$Bots = @(
    "sentinel_risk_orchestrator.py",
    "sentinel_bot.py",
    "sentinel_alpha_compound.py",
    "sentinel_trend.py",
    "sentinel_trade_analytics.py"
)

$allOk = $true
$pyProcs = Get-CimInstance Win32_Process -Filter "Name like 'python%'"

foreach ($bot in $Bots) {
    $proc = $pyProcs | Where-Object {
        $_.CommandLine -match [regex]::Escape($bot)
    } | Select-Object -First 1

    $logFile = Join-Path $LogDir ($bot -replace "\.py$", ".log")
    $lastLog = "aucun log"
    if (Test-Path $logFile) {
        $age = [int]((Get-Date) - (Get-Item $logFile).LastWriteTime).TotalMinutes
        $lastLog = "log ecrit il y a $age min"
    }

    if ($proc) {
        $up = (Get-Date) - $proc.CreationDate
        $upTxt = "{0:d\j\ h\h\ mm\m}" -f $up
        Write-Host ("[OK]     {0,-35} pid {1,-6} up {2,-12} {3}" -f $bot, $proc.ProcessId, $upTxt, $lastLog) -ForegroundColor Green
    } else {
        $allOk = $false
        Write-Host ("[ABSENT] {0,-35} {1}" -f $bot, $lastLog) -ForegroundColor Red
    }
}

$wd = Get-CimInstance Win32_Process -Filter "Name like 'powershell%'" |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match "watchdog\.ps1" } |
    Select-Object -First 1
if ($wd) {
    Write-Host ("[OK]     {0,-35} pid {1}" -f "watchdog.ps1", $wd.ProcessId) -ForegroundColor Green
} else {
    $allOk = $false
    Write-Host ("[ABSENT] {0,-35} les bots ne seront pas relances !" -f "watchdog.ps1") -ForegroundColor Red
}

if ($allOk) { exit 0 } else { exit 1 }
