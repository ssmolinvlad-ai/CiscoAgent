param([Parameter(Mandatory=$true)][string]$OutputDirectory,[int]$InterfaceIndex=0)
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$principal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Administrator privileges required for the physical ARP exercise.'}
$configs=@(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4DefaultGateway.NextHop -ne '0.0.0.0' })
if($InterfaceIndex -eq 0){
    if($configs.Count -ne 1){throw 'Select exactly one physical adapter explicitly.'}
    $config=$configs[0]
}else{
    $config=$configs | Where-Object InterfaceIndex -eq $InterfaceIndex
}
if(-not $config){throw 'Adapter with an IPv4 gateway was not found.'}
$index=[int]$config.InterfaceIndex
$gateway=[string]$config.IPv4DefaultGateway.NextHop
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$original=@(Get-NetNeighbor -InterfaceIndex $index -IPAddress $gateway -ErrorAction SilentlyContinue)
if($original.Count -gt 1){throw 'Ambiguous gateway neighbor state; no changes made.'}
$static=$original.Count -eq 1 -and [string]$original[0].State -eq 'Permanent'
$oldMac=if($original.Count){[string]$original[0].LinkLayerAddress}else{''}
$report=[ordered]@{interface_index=$index;gateway=$gateway;before=@($original | Select-Object InterfaceIndex,IPAddress,LinkLayerAddress,State);empty=$null;learned=$null;wrong_mac=$null;ping_before=$null;ping_after=$null;restored=$false;error=$null}
$restorePath=Join-Path $OutputDirectory 'restore-windows-arp.ps1'
# Independent watchdog survives termination of the agent. Only the selected gateway entry changes.
$restore=@"
`$ErrorActionPreference='Stop'
Remove-NetNeighbor -InterfaceIndex $index -IPAddress '$gateway' -Confirm:`$false -ErrorAction SilentlyContinue
"@
if($static){$restore+="`nNew-NetNeighbor -InterfaceIndex $index -IPAddress '$gateway' -LinkLayerAddress '$oldMac' -State Permanent -PolicyStore ActiveStore | Out-Null"}
else{$restore+="`n& ping.exe -n 1 -w 1000 '$gateway' | Out-Null"}
[IO.File]::WriteAllText($restorePath,$restore,[Text.UTF8Encoding]::new($true))
$watchdog=Join-Path $OutputDirectory 'watchdog-windows-arp.ps1'
[IO.File]::WriteAllText($watchdog,"Start-Sleep -Seconds 10`n& '$($restorePath.Replace("'","''"))'",[Text.UTF8Encoding]::new($true))
$hostExecutable=(Get-Process -Id $PID).Path
$helper=Start-Process $hostExecutable -ArgumentList @('-NoProfile','-NonInteractive','-File',('"'+$watchdog+'"')) -WindowStyle Hidden -PassThru
try{
    # Flush only this gateway entry; unrelated adapters and neighbors are preserved.
    Remove-NetNeighbor -InterfaceIndex $index -IPAddress $gateway -Confirm:$false -ErrorAction SilentlyContinue
    $report.empty=@(Get-NetNeighbor -InterfaceIndex $index -IPAddress $gateway -ErrorAction SilentlyContinue | Select-Object InterfaceIndex,IPAddress,LinkLayerAddress,State)
    $report.ping_before=(& ping.exe -n 2 -w 1000 $gateway | Out-String)
    $report['ping_before_exit']=$LASTEXITCODE
    $report.learned=@(Get-NetNeighbor -InterfaceIndex $index -IPAddress $gateway -ErrorAction SilentlyContinue | Select-Object InterfaceIndex,IPAddress,LinkLayerAddress,State)
    $wrong='02-00-00-00-00-01'
    if($report.learned.Count -and $report.learned[0].LinkLayerAddress -eq $wrong){$wrong='02-00-00-00-00-02'}
    Remove-NetNeighbor -InterfaceIndex $index -IPAddress $gateway -Confirm:$false -ErrorAction SilentlyContinue
    New-NetNeighbor -InterfaceIndex $index -IPAddress $gateway -LinkLayerAddress $wrong -State Permanent -PolicyStore ActiveStore | Out-Null
    $report.wrong_mac=@(Get-NetNeighbor -InterfaceIndex $index -IPAddress $gateway | Select-Object InterfaceIndex,IPAddress,LinkLayerAddress,State)
    $report.ping_after=(& ping.exe -n 2 -w 1000 $gateway | Out-String)
    $report['ping_after_exit']=$LASTEXITCODE
}catch{$report.error=$_.Exception.Message}
finally{
    & $restorePath
    $after=@(Get-NetNeighbor -InterfaceIndex $index -IPAddress $gateway -ErrorAction SilentlyContinue)
    $report.restored=if($static){$after.Count -eq 1 -and $after[0].LinkLayerAddress -eq $oldMac -and [string]$after[0].State -eq 'Permanent'}else{@($after | Where-Object { [string]$_.State -eq 'Permanent' }).Count -eq 0}
    $report | ConvertTo-Json -Depth 7 | Set-Content -LiteralPath (Join-Path $OutputDirectory 'windows-arp.json') -Encoding UTF8
    if($report.restored -and $helper -and -not $helper.HasExited){Stop-Process -Id $helper.Id -ErrorAction SilentlyContinue}
}
if($report.error -or -not $report.restored){exit 1}
