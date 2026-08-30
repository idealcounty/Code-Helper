param(
    [string]$Python = "python",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$WorkspaceRoot = "D:\CodeHelper\workspaces",
    [string]$DataRoot = "D:\CodeHelper\data",
    [string]$TaskName = "Code Helper Server",
    [string]$ServiceUser = "CodeHelperSvc"
)

$ErrorActionPreference = "Stop"
$resolvedProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$environmentFile = Join-Path $resolvedProjectRoot ".env"
$environmentTemplate = Join-Path $resolvedProjectRoot ".env.server.example"
$venvPath = Join-Path $resolvedProjectRoot ".venv-server"
$pythonExecutable = Join-Path $venvPath "Scripts\python.exe"
$runner = Join-Path $resolvedProjectRoot "packaging\run-windows-server.ps1"
$logDirectory = Join-Path $resolvedProjectRoot ".server-logs"

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from PowerShell as Administrator."
}

if (-not (Test-Path -LiteralPath $environmentFile)) {
    Copy-Item -LiteralPath $environmentTemplate -Destination $environmentFile
    Write-Host "Created $environmentFile"
    Write-Host "Fill DEEPSEEK_API_KEY and CODE_HELPER_ACCESS_PASSWORD, then run this script again."
    exit 0
}

$environmentText = Get-Content -LiteralPath $environmentFile -Raw
if ($environmentText -match "(?m)^DEEPSEEK_API_KEY\s*=\s*(?:replace-[^\r\n]*|)$") {
    throw "Configure DEEPSEEK_API_KEY in $environmentFile before installation."
}
if ($environmentText -match "(?m)^CODE_HELPER_ACCESS_PASSWORD\s*=\s*(?:replace-[^\r\n]*|)$") {
    throw "Configure a long CODE_HELPER_ACCESS_PASSWORD in $environmentFile before installation."
}

New-Item -ItemType Directory -Path $WorkspaceRoot -Force | Out-Null
New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

if (-not (Test-Path -LiteralPath $pythonExecutable -PathType Leaf)) {
    & $Python -m venv $venvPath
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the server virtual environment." }
}
& $pythonExecutable -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to update pip." }
& $pythonExecutable -m pip install -e $resolvedProjectRoot
if ($LASTEXITCODE -ne 0) { throw "Failed to install Code Helper." }

$randomBytes = New-Object byte[] 32
$randomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
try { $randomGenerator.GetBytes($randomBytes) } finally { $randomGenerator.Dispose() }
$taskPassword = [Convert]::ToBase64String($randomBytes) + "!aA1"
$securePassword = ConvertTo-SecureString $taskPassword -AsPlainText -Force
$existingUser = Get-LocalUser -Name $ServiceUser -ErrorAction SilentlyContinue
if ($existingUser) {
    Set-LocalUser -Name $ServiceUser -Password $securePassword -PasswordNeverExpires $true
} else {
    New-LocalUser `
        -Name $ServiceUser `
        -Password $securePassword `
        -PasswordNeverExpires `
        -UserMayNotChangePassword `
        -Description "Restricted account for Code Helper" | Out-Null
}

$serviceIdentity = "$env:COMPUTERNAME\$ServiceUser"
& icacls $resolvedProjectRoot /grant "${serviceIdentity}:(OI)(CI)RX" /T /Q | Out-Null
& icacls $WorkspaceRoot /grant "${serviceIdentity}:(OI)(CI)M" /T /Q | Out-Null
& icacls $DataRoot /grant "${serviceIdentity}:(OI)(CI)M" /T /Q | Out-Null
& icacls $logDirectory /grant "${serviceIdentity}:(OI)(CI)M" /T /Q | Out-Null

$actionArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`" -ProjectRoot `"$resolvedProjectRoot`""
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument $actionArguments `
    -WorkingDirectory $resolvedProjectRoot
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 5 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$taskPrincipal = New-ScheduledTaskPrincipal `
    -UserId $serviceIdentity `
    -LogonType Password `
    -RunLevel Limited
$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $taskPrincipal `
    -Description "Run Code Helper as a restricted background service"
Register-ScheduledTask `
    -TaskName $TaskName `
    -InputObject $task `
    -User $serviceIdentity `
    -Password $taskPassword `
    -Force | Out-Null

if (-not (Get-NetFirewallRule -DisplayName "Code Helper Server" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule `
        -DisplayName "Code Helper Server" `
        -Direction Inbound `
        -Protocol TCP `
        -LocalPort 8765 `
        -Action Allow | Out-Null
}

Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started scheduled task: $TaskName"
Write-Host "Workspace root: $WorkspaceRoot"
Write-Host "Logs: $logDirectory\server.log"
Write-Host "Remote URL: http://<server-ip>:8765"
