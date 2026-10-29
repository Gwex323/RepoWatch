param(
    [int]$Port = 8765,
    [string]$Database = "data/repowatch.db",
    [switch]$Background,
    [string]$Manifest
)

$ErrorActionPreference = "Stop"
if ($Manifest) {
    & (Join-Path $PSScriptRoot "managed-start.ps1") -Manifest $Manifest -Background:$Background
    exit $LASTEXITCODE
}
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv/Scripts/python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Create .venv and install the Python project dependencies first."
}
if (-not (Test-Path -LiteralPath (Join-Path $root "dist/index.html"))) {
    throw "Build the WebUI with npm run build first."
}
$selectedPort = $null
foreach ($candidate in $Port..($Port + 20)) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $candidate)
    try {
        $listener.Start()
        $selectedPort = $candidate
        break
    } catch {
        continue
    } finally {
        $listener.Stop()
    }
}
if ($null -eq $selectedPort) { throw "No available loopback port was found." }
$env:REPOWATCH_DB = if ([System.IO.Path]::IsPathRooted($Database)) {
    $Database
} else {
    Join-Path $root $Database
}
$arguments = @("-m", "uvicorn", "repowatch.app:create_app", "--factory", "--host", "127.0.0.1",
    "--port", "$selectedPort")
if ($Background) {
    $logs = Join-Path $root "data/logs"
    New-Item -ItemType Directory -Path $logs -Force | Out-Null
    $process = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $root `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logs "server-$selectedPort.out.log") `
        -RedirectStandardError (Join-Path $logs "server-$selectedPort.err.log")
    $ready = $false
    foreach ($attempt in 1..40) {
        try {
            $session = Invoke-RestMethod -Uri "http://127.0.0.1:$selectedPort/api/session" `
                -TimeoutSec 2
            if ($session.PSObject.Properties.Name -contains "setup_required") {
                $ready = $true
                break
            }
        } catch {
            Start-Sleep -Milliseconds 250
        }
    }
    if (-not $ready) { throw "The server did not become ready. Inspect data/logs." }
    @{ pid = $process.Id; url = "http://127.0.0.1:$selectedPort"; database = $env:REPOWATCH_DB } |
        ConvertTo-Json -Compress
} else {
    Push-Location $root
    try { & $python @arguments } finally { Pop-Location }
}
