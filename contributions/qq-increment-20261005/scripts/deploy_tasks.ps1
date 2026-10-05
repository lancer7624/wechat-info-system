# Register 8 scheduled tasks: export jobs + Claude headless analysis batches.
# Idempotent (-Force). Run: powershell -ExecutionPolicy Bypass -File deploy_tasks.ps1
$ErrorActionPreference = 'Stop'

$ROOT = '<技能安装目录>'
$EXP  = Join-Path $ROOT 'wechat-export'
$PY   = 'C:\Users\<你的用户名>\AppData\Local\Programs\Python\Python313\python.exe'
$BAT  = Join-Path $ROOT 'scripts\run_analysis.bat'

function New-ExportTask($name, $at) {
    $action  = New-ScheduledTaskAction -Execute $PY -Argument '-u daily_export.py' -WorkingDirectory $EXP
    $trigger = New-ScheduledTaskTrigger -Daily -At $at
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Force | Out-Null
    Write-Host ("  {0} @ {1} OK" -f $name, $at)
}

function New-BatchTask($name, $at, $batch) {
    $action  = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ("/c `"{0}`" {1}" -f $BAT, $batch)
    $trigger = New-ScheduledTaskTrigger -Daily -At $at
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Force | Out-Null
    Write-Host ("  {0} @ {1} OK ({2})" -f $name, $at, $batch)
}

New-ExportTask 'WeChatExportNoon'    '12:00'
New-ExportTask 'WeChatExportEvening' '18:00'
New-ExportTask 'WeChatExportNight'   '22:00'
New-BatchTask  'WeChatAnalysisNoon'   '12:10' 'noon'
New-BatchTask  'WeChatAnalysisEvening' '18:10' 'evening'
New-BatchTask  'WeChatTrip'           '21:00' 'trip'
New-BatchTask  'WeChatDaily'          '22:10' 'daily'
New-ExportTask 'WeChatExportFinal'   '23:00'

Write-Host 'All tasks registered'
