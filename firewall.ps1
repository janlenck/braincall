# Als Administrator ausführen. Öffnet SIP/RTP (UDP) und den HTTP-Trigger (TCP) nur im privaten Netz.
$cfg = Get-Content "$PSScriptRoot\config.json" -Raw | ConvertFrom-Json
$sip  = if ($cfg.sip_local_port) { $cfg.sip_local_port } else { 5062 }
$rmin = if ($cfg.rtp_port_min) { $cfg.rtp_port_min } else { 30000 }
$rmax = if ($cfg.rtp_port_max) { $cfg.rtp_port_max } else { 30100 }
$http = if ($cfg.http_port) { $cfg.http_port } else { 8099 }

Get-NetFirewallRule -DisplayName "Claude Telefon*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "Claude Telefon SIP/RTP" -Direction Inbound -Protocol UDP `
  -LocalPort "$sip","$rmin-$rmax" -Action Allow -Profile Private | Out-Null
New-NetFirewallRule -DisplayName "Claude Telefon HTTP" -Direction Inbound -Protocol TCP `
  -LocalPort $http -Action Allow -Profile Private | Out-Null
Write-Host "Regeln angelegt: UDP $sip, $rmin-$rmax / TCP $http (Profil Privat)"
