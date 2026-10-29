# Run as administrator. Opens SIP/RTP (UDP) and the HTTP trigger (TCP) for the private network profile only.
# Adjust the ports if you changed sip_local_port, rtp_port_min/max or http_port in your config.
param(
  [int]$SipPort = 5062,
  [int]$RtpMin  = 30000,
  [int]$RtpMax  = 30100,
  [int]$HttpPort = 8099
)

Get-NetFirewallRule -DisplayName "BrainCall*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "BrainCall SIP/RTP" -Direction Inbound -Protocol UDP `
  -LocalPort "$SipPort","$RtpMin-$RtpMax" -Action Allow -Profile Private | Out-Null
New-NetFirewallRule -DisplayName "BrainCall HTTP" -Direction Inbound -Protocol TCP `
  -LocalPort $HttpPort -Action Allow -Profile Private | Out-Null
Write-Host "Rules created: UDP $SipPort, $RtpMin-$RtpMax / TCP $HttpPort (private profile)"
