# Watchdog de la flotte Sentinel.
# - Verifie toutes les 30 s que chaque bot a un processus vivant, relance sinon
#   (orchestrateur en premier dans la liste).
# - Capture stdout/stderr de chaque bot dans logs/<bot>.log (rotation a 10 Mo).
# - Lance par la tache planifiee "SentinelWatchdog" a l'ouverture de session.

$Root = Split-Path -Parent $PSScriptRoot
$BotsDir = Join-Path $Root "bots"
$LogDir = Join-Path $Root "logs"
$WatchLog = Join-Path $LogDir "watchdog.log"
$CheckSeconds = 30
$Bots = @(
    "sentinel_risk_orchestrator.py",
    "sentinel_bot.py",
    "sentinel_alpha_compound.py",
    "sentinel_trend.py"
)

New-Item -ItemType Directory -Force $LogDir | Out-Null

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Add-Content $WatchLog
}

# garde anti-doublon : un seul watchdog a la fois
$twin = Get-CimInstance Win32_Process -Filter "Name like 'powershell%'" |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match "watchdog\.ps1" }
if ($twin) {
    Write-Log "watchdog deja actif (pid $($twin[0].ProcessId)), sortie."
    exit 0
}

Write-Log "watchdog demarre (pid $PID)"
while ($true) {
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'"
    foreach ($bot in $Bots) {
        $running = $procs | Where-Object {
            $_.CommandLine -match [regex]::Escape($bot)
        }
        if (-not $running) {
            $logFile = Join-Path $LogDir ($bot -replace "\.py$", ".log")
            Write-Log "$bot absent -> relance"
            Start-Process cmd `
                -ArgumentList "/c python -u $bot >> `"$logFile`" 2>&1" `
                -WorkingDirectory $BotsDir -WindowStyle Hidden
            Start-Sleep -Seconds 3
        }
    }
    Get-ChildItem $LogDir -Filter *.log |
        Where-Object Length -gt 10MB | ForEach-Object {
            Move-Item $_.FullName "$($_.FullName).1" -Force
            Write-Log "rotation $($_.Name)"
        }
    Start-Sleep -Seconds $CheckSeconds
}
