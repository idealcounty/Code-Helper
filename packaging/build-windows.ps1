param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$previousPythonUserBase = $env:PYTHONUSERBASE
$buildUserBase = Join-Path ([System.IO.Path]::GetTempPath()) "code-helper-pyinstaller-userbase"

Push-Location $projectRoot
try {
    # Some managed Windows profiles expose an unreadable default user
    # site-packages directory.  PyInstaller inspects that directory even when
    # all dependencies are installed globally, so give the build an isolated,
    # writable user base and restore the caller's environment afterwards.
    New-Item -ItemType Directory -Path $buildUserBase -Force | Out-Null
    $env:PYTHONUSERBASE = $buildUserBase

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
    $env:PYTHONUSERBASE = $previousPythonUserBase
    Pop-Location
}
