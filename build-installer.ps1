param([string]$Compiler = "$PSScriptRoot\build\tools\inno6\ISCC.exe")
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath $Compiler)) { throw 'Install Inno Setup 6.4.3 or pass -Compiler with ISCC.exe path.' }
powershell -NoProfile -File .\native\install-runtime.ps1 -VerifyOnly
if ($LASTEXITCODE -ne 0) { throw 'Runtime package verification failed.' }
powershell -NoProfile -File .\build-connector.ps1
if ($LASTEXITCODE -ne 0) { throw 'Connector build failed.' }
& $Compiler .\installer\Portway.iss
if ($LASTEXITCODE -ne 0) { throw 'Portway installer build failed.' }
Write-Host 'Built dist/PortwayConnectorSetup.exe with signed host and guest runtime packages.'
