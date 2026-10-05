# Add AtLogOn trigger to export tasks via XML import.
$ErrorActionPreference = 'Stop'

foreach ($n in @('WeChatExportNoon','WeChatExportEvening','WeChatExportNight','WeChatExportFinal')) {
    $xml = Export-ScheduledTask -TaskName $n
    if ($xml -match '<LogonTrigger>') {
        Write-Host "$n already has logon"
        continue
    }
    $logon = '<LogonTrigger><Enabled>true</Enabled></LogonTrigger>'
    $idx = $xml.IndexOf('<Triggers>')
    if ($idx -lt 0) { Write-Host "$n no Triggers node"; continue }
    $insertAt = $xml.IndexOf('>', $idx) + 1
    $newXml = $xml.Insert($insertAt, $logon)
    Register-ScheduledTask -TaskName $n -Xml $newXml -Force | Out-Null
    Write-Host "$n logon added"
}
Write-Host 'done'
