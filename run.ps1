$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if (-not (Test-Path '.venv/Scripts/python.exe')) { throw 'Run setup.ps1 first.' }
& ./.venv/Scripts/python.exe server.py
