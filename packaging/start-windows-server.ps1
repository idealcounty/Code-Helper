param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$runner = Join-Path $resolvedProjectRoot "packaging\run-windows-server.ps1"
$logDirectory = Join-Path $resolvedProjectRoot ".server-logs"
$pidFile = Join-Path $logDirectory "server.pid"

if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
    throw "Server runner is missing: $runner"
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
if (Test-Path -LiteralPath $pidFile) {
    $existingPid = [int](Get-Content -LiteralPath $pidFile -Raw)
    if (Get-Process -Id $existingPid -ErrorAction SilentlyContinue) {
        Write-Host "Code Helper server is already running (PID $existingPid)."
        exit 0
    }
}

$arguments = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", $runner,
    "-ProjectRoot", $resolvedProjectRoot
)
$process = Start-Process `
    -FilePath "powershell.exe" `
    -ArgumentList $arguments `
    -WorkingDirectory $resolvedProjectRoot `
    -WindowStyle Hidden `
    -PassThru
Set-Content -LiteralPath $pidFile -Value $process.Id -Encoding ascii
Write-Host "Code Helper server started in the background (PID $($process.Id))."
Write-Host "Logs: $logDirectory\server.log"
