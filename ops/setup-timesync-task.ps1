# Creates the elevated on-demand task "SentinelTimeSync" (run ONCE from
# an elevated PowerShell). The watchdog (which runs Limited) triggers it
# with Start-ScheduledTask whenever the NTP service W32Time stops: the
# task restarts the service and forces a resync. No schedule: on demand.

$name = "SentinelTimeSync"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument (
    '-NoProfile -WindowStyle Hidden -Command ' +
    '"Set-Service W32Time -StartupType Automatic; ' +
    'Start-Service W32Time; w32tm /resync /force"')
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME `
    -LogonType Interactive -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (
    New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName $name -Action $action `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "$name task registered (RunLevel Highest, on demand)."
