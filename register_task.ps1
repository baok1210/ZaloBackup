# Registers a daily 07:30 auto-crawl task for Zalo Backup.
# - Starts ZaloAutoCrawl.exe (which talks to the WebUI server over HTTP).
# - StartWhenAvailable: if the PC was off at 07:30, the run happens at boot.
# - ExecutionTimeLimit 8h (the runner itself caps jobs at 6h).
# - IgnoreNew: an overlapping run is skipped, never doubled.
$exe = 'H:\zalo-backup\ZaloAutoCrawl.exe'
$vbs = 'H:\zalo-backup\run_auto_crawl.vbs'
foreach ($f in @($exe, $vbs)) {
    if (-not (Test-Path $f)) { throw "$f not found" }
}

$action    = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument "`"$vbs`"" -WorkingDirectory 'H:\zalo-backup'
$trigger   = New-ScheduledTaskTrigger -Daily -At 07:30
$settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 8) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName 'ZaloBackup AutoCrawl' -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force
Write-Host "Registered 'ZaloBackup AutoCrawl' (daily 07:30, catch-up if PC was off)."
