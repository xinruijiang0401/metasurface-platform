param(
    [ValidateRange(1024, 65535)][int]$Port = 8765,
    [switch]$NoBrowser,
    [switch]$Foreground
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$workbenchPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $workbenchPython)) {
    throw 'Run bootstrap.ps1 first.'
}
$workbenchUrl = "http://127.0.0.1:$Port/"
$workbenchRuntime = Join-Path $PSScriptRoot '.runtime'
$env:PYTHONUTF8 = '1'

function Get-WorkbenchStatus {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ($workbenchUrl + 'api/status') -TimeoutSec 2 -ErrorAction Stop
    } catch {
        return $null
    }
    try {
        $status = $response.Content | ConvertFrom-Json
    } catch {
        throw "Port $Port belongs to another service. Choose a different -Port."
    }
    $serverHeader = $response.Headers['Server'] -join ' '
    if ($serverHeader -notmatch '^Metasurface/' -or $status.available -ne $true -or -not $status.default_config) {
        throw "Port $Port belongs to another service. Choose a different -Port."
    }
    return $status
}

$workbenchStatus = Get-WorkbenchStatus
if (-not $workbenchStatus) {
    if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
        throw "Port $Port is occupied or the existing service is unhealthy. Choose a different -Port."
    }
    if ($Foreground) {
        Write-Output "Foreground service: $workbenchUrl (keep this terminal open)"
        & $workbenchPython -u -m metasurface_platform serve --port $Port
        exit $LASTEXITCODE
    }
    New-Item -ItemType Directory -Path $workbenchRuntime -Force | Out-Null
    $stdoutLog = Join-Path $workbenchRuntime "workbench-$Port.stdout.log"
    $stderrLog = Join-Path $workbenchRuntime "workbench-$Port.stderr.log"
    $workbenchProcess = Start-Process -FilePath $workbenchPython -ArgumentList @('-u', '-m', 'metasurface_platform', 'serve', '--port', "$Port") -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog -PassThru
    $startupTimer = [System.Diagnostics.Stopwatch]::StartNew()
    while (-not $workbenchStatus -and $startupTimer.Elapsed.TotalSeconds -lt 20) {
        if ($workbenchProcess.HasExited) {
            throw "Service exited during startup. See $stderrLog"
        }
        $workbenchStatus = Get-WorkbenchStatus
        if (-not $workbenchStatus) { Start-Sleep -Milliseconds 250 }
    }
    if (-not $workbenchStatus) {
        throw "Service did not become ready within 20 seconds. See $stderrLog"
    }
    $workbenchListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    $workbenchServiceId = if ($workbenchListener) { $workbenchListener.OwningProcess } else { $workbenchProcess.Id }
    $workbenchServiceId | Set-Content -LiteralPath (Join-Path $workbenchRuntime "workbench-$Port.pid") -Encoding ascii
    Write-Output "Background service started. Logs: $workbenchRuntime"
} else {
    Write-Output 'The workbench is already running; reusing it.'
}

Write-Output "Ready: $workbenchUrl"
if (-not $NoBrowser) { Start-Process $workbenchUrl }
