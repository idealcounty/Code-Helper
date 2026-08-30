param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

Push-Location $projectRoot
try {
    & $Python -m pip install -e ".[desktop]"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install desktop build dependencies."
    }

    & $Python -m PyInstaller --noconfirm --clean "packaging/coding-agent.spec"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }

    Copy-Item ".env.example" "dist/code-helper/.env.example" -Force
    Write-Host "Built: $projectRoot\dist\code-helper\code-helper.exe"
}
finally {
    Pop-Location
}
