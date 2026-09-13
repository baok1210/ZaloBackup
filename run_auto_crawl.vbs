' Zalo Backup — hidden launcher for the daily auto-crawl task.
' Runs ZaloAutoCrawl.exe with no visible window and waits for it to finish,
' so Task Scheduler reports the real duration and last-run result.
Dim shell
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "H:\zalo-backup"
shell.Run """H:\zalo-backup\ZaloAutoCrawl.exe""", 0, True
