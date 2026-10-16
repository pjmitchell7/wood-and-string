$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$free = (Get-PSDrive -Name ((Get-Item $PSScriptRoot).PSDrive.Name)).Free
if ($free -lt 9GB) { throw 'Setup needs 1 GB workspace plus an 8 GB free-space reserve.' }
if (-not (Test-Path '.venv/Scripts/python.exe')) { python -m venv .venv }
& ./.venv/Scripts/python.exe -m pip install -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Dependency setup failed.' }
Write-Host 'Local player ready. Run run.ps1, then open http://127.0.0.1:8765. Audio sample acquisition is documented separately.'
