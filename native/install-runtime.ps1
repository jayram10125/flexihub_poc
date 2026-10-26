param([switch]$VerifyOnly)
$ErrorActionPreference = 'Stop'
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'packages.lock.json') -Raw | ConvertFrom-Json
$packageDir = Join-Path $PSScriptRoot 'packages'
$logDir = Join-Path $PSScriptRoot 'logs'
New-Item -ItemType Directory -Force -Path $packageDir,$logDir | Out-Null
if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'This runtime package supports x64 Windows only.' }
foreach ($package in $manifest.packages) {
    $target = Join-Path $packageDir $package.file
    if (-not (Test-Path -LiteralPath $target)) {
        Invoke-WebRequest -UseBasicParsing -Uri $package.url -OutFile $target
    }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $package.sha256) {
        throw "Package hash mismatch: $($package.file)"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $target
    if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate.Subject.Contains($package.publisher)) {
        throw "Untrusted installer signature/publisher: $($package.file)"
    }
    Write-Output "Verified $($package.role): $($package.file)"
}
if ($VerifyOnly) { exit 0 }
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Administrator privileges are required to install the signed driver runtime.'
}
# Do not alter Secure Boot, test-signing or trust stores. Windows retains the
# authority to reject any kernel driver that does not satisfy its signing policy.
$rebootRequired = $false
foreach ($package in $manifest.packages) {
    $target = Join-Path $packageDir $package.file
    $logFile = Join-Path $logDir ($package.role + '-install.log')
    if ($package.role -eq 'host') {
        $process = Start-Process -FilePath 'msiexec.exe' -ArgumentList @('/i',('"' + $target + '"'),'/qn','/norestart','/L*v',('"' + $logFile + '"')) -WindowStyle Hidden -Wait -PassThru
    } else {
        $process = Start-Process -FilePath $target -ArgumentList @('/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/RESTARTEXITCODE=3010','/TYPE=compact','/TASKS=vcredist',('/LOG="' + $logFile + '"')) -WindowStyle Hidden -Wait -PassThru
    }
    if ($process.ExitCode -notin @(0,3010)) { throw "$($package.role) installation failed with $($process.ExitCode). See $logFile" }
    if ($process.ExitCode -eq 3010) { $rebootRequired = $true }
}
$hostService = Get-Service -Name usbipd -ErrorAction Stop
# The runtime hardware ID is ROOT\USBIP_WIN2\UDE, but its Windows instance
# is ROOT\USB\nnnn. Query hardware IDs instead of assuming instance-ID format.
$guest = Get-CimInstance Win32_PnPEntity | Where-Object { $_.HardwareID -contains 'ROOT\USBIP_WIN2\UDE' }
# The upstream host installer opens raw port 3240 on the LAN. Portway uses a
# TLS endpoint, so remove that upstream exception; loopback remains usable.
Get-NetFirewallRule -Name 'usbipd' -ErrorAction SilentlyContinue | Disable-NetFirewallRule | Out-Null
$result = [pscustomobject]@{
    HostService=$hostService.Status.ToString()
    GuestControllers=@($guest | Select-Object Status,PNPDeviceID)
    RebootRequired=$rebootRequired
    HardwareAcceptancePassed=$false
}
$result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $logDir 'install-result.json') -Encoding UTF8
if (-not $guest) { throw 'Guest virtual controller was not detected. Inspect installer log; a reboot may be required.' }
$result | ConvertTo-Json -Depth 5
