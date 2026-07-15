# Watchdog de la flotte Sentinel.
# - Verifie toutes les 30 s que chaque bot a un processus vivant, relance sinon
#   (orchestrateur en premier dans la liste).
# - Capture stdout/stderr de chaque bot dans logs/<bot>.log (rotation a 10 Mo).
# - Ecrit logs/status.html a chaque cycle (page auto-rafraichie, a ouvrir
#   dans un navigateur pour surveiller la flotte).
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

$StatusHtml = Join-Path $LogDir "status.html"

function Write-StatusPage($rows) {
    $tr = ($rows | ForEach-Object {
        $cls = if ($_.Ok) { "ok" } else { "ko" }
        $etat = if ($_.Ok) { "OK" } else { "RELANCE" }
        "<tr class='$cls'><td>$etat</td><td>$($_.Bot)</td><td>$($_.ProcId)</td><td>$($_.Up)</td><td>$($_.Log)</td></tr>"
    }) -join "`n"
    $events = ""
    if (Test-Path $WatchLog) {
        $events = (Get-Content $WatchLog -Tail 10) -join "`n"
    }
    @"
<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>Sentinel - etat de la flotte</title>
<style>
 body { font-family: Segoe UI, sans-serif; margin: 2em; background: #1b1e24; color: #d8dde6; }
 h1 { font-size: 1.3em; } small { color: #8a93a3; }
 table { border-collapse: collapse; margin-top: 1em; }
 td, th { padding: .45em .9em; border-bottom: 1px solid #333a45; text-align: left; }
 tr.ok td:first-child { color: #4cc36a; font-weight: bold; }
 tr.ko td:first-child { color: #e05555; font-weight: bold; }
 pre { background: #14161b; padding: 1em; margin-top: 1.5em; color: #8a93a3; }
</style>
</head><body>
<h1>Flotte Sentinel <small>mise a jour $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (auto-refresh 30 s, watchdog pid $PID)</small></h1>
<table>
<tr><th>Etat</th><th>Bot</th><th>PID</th><th>Uptime</th><th>Dernier log</th></tr>
$tr
</table>
<h1>Derniers evenements watchdog</h1>
<pre>$events</pre>
</body></html>
"@ | Out-File $StatusHtml -Encoding utf8
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
    $rows = @()
    foreach ($bot in $Bots) {
        $running = $procs | Where-Object {
            $_.CommandLine -match [regex]::Escape($bot)
        } | Select-Object -First 1
        $logFile = Join-Path $LogDir ($bot -replace "\.py$", ".log")
        $logInfo = "aucun log"
        if (Test-Path $logFile) {
            $min = [int]((Get-Date) - (Get-Item $logFile).LastWriteTime).TotalMinutes
            $logInfo = "il y a $min min"
        }
        if ($running) {
            $up = "{0:d\j\ h\h\ mm\m}" -f ((Get-Date) - $running.CreationDate)
            $rows += [pscustomobject]@{
                Bot = $bot; Ok = $true
                ProcId = $running.ProcessId; Up = $up; Log = $logInfo
            }
        } else {
            Write-Log "$bot absent -> relance"
            Start-Process cmd `
                -ArgumentList "/c python -u $bot >> `"$logFile`" 2>&1" `
                -WorkingDirectory $BotsDir -WindowStyle Hidden
            $rows += [pscustomobject]@{
                Bot = $bot; Ok = $false
                ProcId = "-"; Up = "-"; Log = $logInfo
            }
            Start-Sleep -Seconds 3
        }
    }
    Get-ChildItem $LogDir -Filter *.log |
        Where-Object Length -gt 10MB | ForEach-Object {
            Move-Item $_.FullName "$($_.FullName).1" -Force
            Write-Log "rotation $($_.Name)"
        }
    Write-StatusPage $rows
    Start-Sleep -Seconds $CheckSeconds
}
