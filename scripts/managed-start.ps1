param(
    [Parameter(Mandatory = $true)][string]$Manifest,
    [switch]$Background
)

$ErrorActionPreference = "Stop"
$manifestPath = (Resolve-Path -LiteralPath $Manifest).Path
$release = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$entry = Join-Path $release.code_root "scripts/managed.py"
& $release.python -I $entry deploy --manifest $manifestPath
if ($LASTEXITCODE -ne 0) { throw "Managed deployment was blocked. No supervisor started." }
if ($Background) {
    $arguments = @("-I", "`"$entry`"", "supervise", "--state-root", "`"$($release.state_root)`"")
    $process = Start-Process -FilePath $release.python -ArgumentList $arguments `
        -WorkingDirectory $release.code_root -WindowStyle Hidden -PassThru
    @{ supervisor_pid = $process.Id; port = $release.port; release_id = $release.id } |
        ConvertTo-Json -Compress
} else {
    & $release.python -I $entry supervise --state-root $release.state_root
}
