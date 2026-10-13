$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$runtimePath = Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$uvPath = Join-Path $runtimePath 'uv\uv.exe'
if (-not (Test-Path -LiteralPath $uvPath)) {
    $archivePath = Join-Path $runtimePath 'uv-0.9.2-windows.zip'
    Invoke-WebRequest -Uri 'https://github.com/astral-sh/uv/releases/download/0.9.2/uv-x86_64-pc-windows-msvc.zip' -OutFile $archivePath
    Expand-Archive -LiteralPath $archivePath -DestinationPath (Join-Path $runtimePath 'uv') -Force
}
$env:UV_PYTHON_INSTALL_DIR = Join-Path $runtimePath 'python'
$env:UV_PYTHON_BIN_DIR = Join-Path $runtimePath 'bin'
$env:UV_CACHE_DIR = Join-Path $runtimePath 'cache'
$env:PYTHONUTF8 = '1'
& $uvPath python install 3.12.11 --no-progress --quiet
if ($LASTEXITCODE -ne 0) { throw 'Python installation failed' }
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $uvPath venv --python 3.12.11 .venv --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Environment creation failed' }
}
if (Test-Path -LiteralPath 'requirements-lock.txt') {
    & $uvPath pip install --python '.venv\Scripts\python.exe' -r 'requirements-lock.txt' --quiet
    if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed' }
    & $uvPath pip install --python '.venv\Scripts\python.exe' --no-deps -e . --quiet
} else {
    & $uvPath pip install --python '.venv\Scripts\python.exe' -e '.[test]' --quiet
}
if ($LASTEXITCODE -ne 0) { throw 'Package installation failed' }
Write-Output 'Ready. Run: .\.venv\Scripts\python.exe -m metasurface_platform serve --port 8765'
