# Register QQ auto-refresh scheduled task: every 20 min + at logon
$ErrorActionPreference = 'Stop'
$PY = 'C:\Users\<你的用户名>\AppData\Local\Programs\Python\Python313\python.exe'
$WD = '<技能安装目录>\wechat-export'

$action  = New-ScheduledTaskAction -Execute $PY -Argument '-X utf8 qq_auto_refresh.py' -WorkingDirectory $WD
$t1 = New-ScheduledTaskTrigger -Once -At '2026-01-01T00:00' -RepetitionInterval (New-TimeSpan -Minutes 20) -RepetitionDuration (New-TimeSpan -Days 3650)
$t2 = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName 'QQAutoRefresh' -Action $action -Trigger $t1, $t2 -Force | Out-Null
Write-Host 'QQAutoRefresh registered: every 20min + at logon'
