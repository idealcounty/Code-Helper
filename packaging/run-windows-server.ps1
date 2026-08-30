param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"
$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$pythonExecutable = Join-Path $resolvedProjectRoot ".venv-server\Scripts\python.exe"
$logDirectory = Join-Path $resolvedProjectRoot ".server-logs"

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    throw "Server virtual environment is missing. Run packaging/install-windows-server.ps1 first."
}

New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
Set-Location -LiteralPath $resolvedProjectRoot

$timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
Add-Content -LiteralPath (Join-Path $logDirectory "server.log") -Value "[$timestamp] Starting Code Helper server"
& $pythonExecutable -m coding_agent.web.app *>> (Join-Path $logDirectory "server.log")
exit $LASTEXITCODE
