param([switch]$HardenFirewall)
$ErrorActionPreference = 'Stop'
if ($HardenFirewall) {
    Get-NetFirewallRule -Name 'usbipd' -ErrorAction SilentlyContinue | Disable-NetFirewallRule | Out-Null
}
$guest = @(Get-CimInstance Win32_PnPEntity | Where-Object { $_.HardwareID -contains 'ROOT\USBIP_WIN2\UDE' })
$signed = @(Get-CimInstance Win32_PnPSignedDriver | Where-Object { $guest.PNPDeviceID -contains $_.DeviceID })
$result = [pscustomobject]@{
    HostService=(Get-Service usbipd -ErrorAction Stop).Status.ToString()
    GuestControllers=@($guest | Select-Object Status,PNPDeviceID)
    GuestSigning=@($signed | Select-Object IsSigned,Signer,InfName)
    HardwareAcceptancePassed=$false
}
$result | ConvertTo-Json -Depth 5
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'logs\runtime-verification.json') -Encoding UTF8
if (-not $guest -or -not $signed -or @($signed | Where-Object { -not $_.IsSigned }).Count -gt 0) {
    throw 'Signed guest controller not found; restart Windows if the installer requested it.'
}
