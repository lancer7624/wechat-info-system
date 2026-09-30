# 气泡提示：recorder.py 调用，不抢焦点，几秒自动消失
param([string]$Title, [string]$Msg)
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(6000, $Title, $Msg, "Info")
Start-Sleep -Seconds 7
$n.Dispose()
