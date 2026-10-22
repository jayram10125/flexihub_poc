param()
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
python -m PyInstaller --noconfirm --clean --windowed --onefile --name PortwayConnector --distpath dist --workpath build/pyinstaller --specpath build connector/app.pyw
if ($LASTEXITCODE -ne 0) { throw 'Connector build failed.' }
Write-Host 'Built dist/PortwayConnector.exe'
