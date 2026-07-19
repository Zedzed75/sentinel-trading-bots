# Sentinel fleet watchdog.
# - Checks every 30 s that each bot (and the mobile dashboard) has a live
#   process, restarts otherwise (orchestrator first in the list).
# - Captures each bot's stdout/stderr into logs/<bot>.log (10 MB rotation).
# - Writes logs/status.html on each cycle (auto-refreshing page, open it
#   in a browser to monitor the fleet).
# - Checks the NTP service (W32Time) and requests a restart through the
#   elevated on-demand task "SentinelTimeSync" if it stops.
# - Launched by the "SentinelWatchdog" scheduled task at session logon.

$Root = Split-Path -Parent $PSScriptRoot
$BotsDir = Join-Path $Root "bots"
$LogDir = Join-Path $Root "logs"
$WatchLog = Join-Path $LogDir "watchdog.log"
$CheckSeconds = 30
$Bots = @(
    "sentinel_risk_orchestrator.py",
    "sentinel_bot.py",
    "sentinel_alpha_compound.py",
    "sentinel_trend.py",
    "sentinel_trade_analytics.py",
    "sentinel_telegram.py",
    "sentinel_macro_analyst.py",
    "sentinel_arbitrage.py",
    "sentinel_dashboard.py"
)
# sentinel_dashboard.py lives at the repo root; every bot lives in bots/.
function Get-BotDir($bot) {
    if ($bot -eq "sentinel_dashboard.py") { return $Root }
    return $BotsDir
}

# NTP guard: the clock must stay disciplined (issue #32 context). This
# watchdog runs Limited, so W32Time is restarted through the elevated
# on-demand scheduled task SentinelTimeSync; Telegram alert fires on
# the down transition only (no spam every 30 s cycle).
$TimeSyncTask = "SentinelTimeSync"
$script:NtpWasDown = $false

function Check-TimeService {
    $svc = Get-Service W32Time -ErrorAction SilentlyContinue
    $down = (-not $svc) -or ($svc.Status -ne "Running")
    if ($down) {
        if (-not $script:NtpWasDown) {
            Write-Log "W32Time stopped -> Start-ScheduledTask $TimeSyncTask"
            Send-Telegram ("ALERT: NTP (W32Time) stopped - restart " +
                "requested via the $TimeSyncTask task")
        }
        Start-ScheduledTask -TaskName $TimeSyncTask `
            -ErrorAction SilentlyContinue
    } elseif ($script:NtpWasDown) {
        Write-Log "W32Time running again"
    }
    $script:NtpWasDown = $down
}
# Max heartbeat age (logs/<bot>.hb, written after each successful cycle).
# Beyond that: process alive but frozen -> kill + restart. Bot 5 has a
# 15-min cycle, bot 6 sometimes waits for a token: adapted thresholds.
# The dashboard writes no heartbeat (request-driven): process check only.
$HbLimitSec = @{
    "sentinel_risk_orchestrator.py" = 300
    "sentinel_bot.py"               = 300
    "sentinel_alpha_compound.py"    = 300
    "sentinel_trend.py"             = 300
    "sentinel_trade_analytics.py"   = 2700
    "sentinel_telegram.py"          = 300
    "sentinel_macro_analyst.py"     = 300
    "sentinel_arbitrage.py"         = 300
}

New-Item -ItemType Directory -Force $LogDir | Out-Null

function Write-Log($msg) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg" | Add-Content $WatchLog
}

# Telegram alert (token in bots/telegram_config.json, chat_id captured
# by sentinel_telegram.py in telegram_state.json). Silent if missing.
function Send-Telegram($text) {
    try {
        $cfg = Get-Content (Join-Path $BotsDir "telegram_config.json") `
            -Raw -ErrorAction Stop | ConvertFrom-Json
        $st = Get-Content (Join-Path $BotsDir "telegram_state.json") `
            -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($cfg.token -and $st.chat_id) {
            Invoke-RestMethod -Method Post `
                -Uri "https://api.telegram.org/bot$($cfg.token)/sendMessage" `
                -Body @{ chat_id = $st.chat_id; text = $text } | Out-Null
        }
    } catch { }
}

$StatusHtml = Join-Path $LogDir "status.html"

function Write-StatusPage($rows) {
    $tr = ($rows | ForEach-Object {
        $cls = if ($_.Ok) { "ok" } else { "ko" }
        $state = if ($_.Ok) { "OK" } else { "RESTARTED" }
        "<tr class='$cls'><td>$state</td><td>$($_.Bot)</td><td>$($_.ProcId)</td><td>$($_.Up)</td><td>$($_.Log)</td></tr>"
    }) -join "`n"
    $events = ""
    if (Test-Path $WatchLog) {
        $events = (Get-Content $WatchLog -Tail 10) -join "`n"
    }
    @"
<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="30">
<title>Sentinel - fleet status</title>
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
<h1>Sentinel fleet <small>updated $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') (auto-refresh 30 s, watchdog pid $PID)</small></h1>
<table>
<tr><th>State</th><th>Bot</th><th>PID</th><th>Uptime</th><th>Last log</th></tr>
$tr
</table>
<h1>Latest watchdog events</h1>
<pre>$events</pre>
</body></html>
"@ | Out-File $StatusHtml -Encoding utf8
}

# anti-duplicate guard: only one watchdog at a time
$twin = Get-CimInstance Win32_Process -Filter "Name like 'powershell%'" |
    Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match "watchdog\.ps1" }
if ($twin) {
    Write-Log "watchdog already running (pid $($twin[0].ProcessId)), exiting."
    exit 0
}

Write-Log "watchdog started (pid $PID)"
while ($true) {
    $procs = Get-CimInstance Win32_Process -Filter "Name like 'python%'"
    $rows = @()
    foreach ($bot in $Bots) {
        $running = $procs | Where-Object {
            $_.CommandLine -match [regex]::Escape($bot)
        } | Select-Object -First 1
        $logFile = Join-Path $LogDir ($bot -replace "\.py$", ".log")

        # heartbeat: alive but frozen (present AND older than the limit)
        if ($running) {
            $hb = Join-Path $LogDir ($bot -replace "\.py$", ".hb")
            if (Test-Path $hb) {
                $age = ((Get-Date) - (Get-Item $hb).LastWriteTime).TotalSeconds
                if ($age -gt $HbLimitSec[$bot]) {
                    Write-Log ("$bot frozen (heartbeat {0:n0}s) -> kill" -f $age)
                    Send-Telegram ("ALERT: $bot frozen (no cycle for " +
                        "{0:n0} min) - forced restart" -f ($age / 60))
                    Stop-Process -Id $running.ProcessId -Force `
                        -ErrorAction SilentlyContinue
                    Remove-Item $hb -Force -ErrorAction SilentlyContinue
                    $running = $null
                }
            }
        }
        $logInfo = "no log"
        if (Test-Path $logFile) {
            $min = [int]((Get-Date) - (Get-Item $logFile).LastWriteTime).TotalMinutes
            $logInfo = "$min min ago"
        }
        if ($running) {
            $up = "{0:d\d\ h\h\ mm\m}" -f ((Get-Date) - $running.CreationDate)
            $rows += [pscustomobject]@{
                Bot = $bot; Ok = $true
                ProcId = $running.ProcessId; Up = $up; Log = $logInfo
            }
        } else {
            Write-Log "$bot missing -> restart"
            Send-Telegram "ALERT: $bot was stopped - restarted by the watchdog"
            Start-Process cmd `
                -ArgumentList "/c python -u $bot >> `"$logFile`" 2>&1" `
                -WorkingDirectory (Get-BotDir $bot) -WindowStyle Hidden
            $rows += [pscustomobject]@{
                Bot = $bot; Ok = $false
                ProcId = "-"; Up = "-"; Log = $logInfo
            }
            Start-Sleep -Seconds 3
        }
    }
    Check-TimeService
    Get-ChildItem $LogDir -Filter *.log |
        Where-Object Length -gt 10MB | ForEach-Object {
            Move-Item $_.FullName "$($_.FullName).1" -Force
            Write-Log "rotated $($_.Name)"
        }
    Write-StatusPage $rows
    Start-Sleep -Seconds $CheckSeconds
}
