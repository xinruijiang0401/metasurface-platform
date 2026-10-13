$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    throw 'Run bootstrap.ps1 first.'
}
$env:PYTHONUTF8 = '1'
& '.\.venv\Scripts\python.exe' -m metasurface_platform serve --port 8765
