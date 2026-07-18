# Sentinel fleet status: one OK/MISSING per bot + watchdog.
# Usage: powershell -File ops\status.ps1
# Exit code: 0 if everything runs, 1 otherwise (scriptable).

$Root = Split-Path -Parent $PSScriptRoot
$LogDir = Join-Path $Root "logs"
$Bots = @(
    "sentinel_risk_orchestrator.py",
    "sentinel_bot.py",
    "sentinel_alpha_compound.py",
    "sentinel_trend.py",
    "sentinel_trade_analytics.py",
    "sentinel_telegram.py",
    "sentinel_macro_analyst.py",
    "sentinel_arbitrage.py"
)

$allOk = $true
$pyProcs = Get-CimInstance Win32_Process -Filter "Name like 'python%'"

foreach ($bot in $Bots) {
    $proc = $pyProcs | Where-Object {
        $_.CommandLine -match [regex]::Escape($bot)
    } | Select-Object -First 1

    $logFile = Join-Path $LogDir ($bot -replace "\.py$", ".log")
    $lastLog = "no log"
    if (Test-Path $logFile) {
        $age = [int]((Get-Date) - (Get-Item $logFile).LastWriteTime).TotalMinutes
        $lastLog = "log written $age min ago"
    }
    $hbFile = Join-Path $LogDir ($bot -replace "\.py$", ".hb")
    $hb = "hb ?"
    if (Test-Path $hbFile) {
        $hb = "hb {0}s" -f [int]((Get-Date) - (Get-Item $hbFile).LastWriteTime).TotalSeconds
    }

    if ($proc) {
        $up = (Get-Date) - $proc.CreationDate
        $upTxt = "{0:d\d\ h\h\ mm\m}" -f $up
        Write-Host ("[OK]      {0,-35} pid {1,-6} up {2,-12} {3,-8} {4}" -f $bot, $proc.ProcessId, $upTxt, $hb, $lastLog) -ForegroundColor Green
    } else {
        $allOk = $false
        Write-Host ("[MISSING] {0,-35} {1}" -f $bot, $lastLog) -ForegroundColor Red
    }
}

$wd = Get-CimInstance Win32_Process -Filter "Name like 'powershell%'" |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match "watchdog\.ps1" } |
    Select-Object -First 1
if ($wd) {
    Write-Host ("[OK]      {0,-35} pid {1}" -f "watchdog.ps1", $wd.ProcessId) -ForegroundColor Green
} else {
    $allOk = $false
    Write-Host ("[MISSING] {0,-35} bots will not be restarted!" -f "watchdog.ps1") -ForegroundColor Red
}

if ($allOk) { exit 0 } else { exit 1 }
