# Removes the Zalo Backup auto-crawl scheduled task.
Unregister-ScheduledTask -TaskName 'ZaloBackup AutoCrawl' -Confirm:$false
Write-Host "Task 'ZaloBackup AutoCrawl' removed."
