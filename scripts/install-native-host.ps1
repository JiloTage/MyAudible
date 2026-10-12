<# Register this checkout's launcher for the current user's Chrome. #>
[CmdletBinding()]
param(
    [ValidatePattern('^[a-p]{32}$')]
    [string] $ExtensionId = '',
    [ValidateSet('irodori-tts-mf', 'irodori-tts-mf:q8_0', 'irodori-tts', 'irodori-tts:q8_0')]
    [string] $Model = 'irodori-tts-mf'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3
if ($env:OS -ne 'Windows_NT') { throw 'Run this script on Windows.' }
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run scripts\setup-windows.ps1 first.' }
& $venvPython -c 'import myaudible.config, uvicorn'
if ($LASTEXITCODE -ne 0) { throw 'The backend dependencies are missing. Run scripts\setup-windows.ps1 first.' }
$commandShell = [Environment]::GetEnvironmentVariable('ComSpec', 'User')
if (-not $commandShell) { $commandShell = [Environment]::GetEnvironmentVariable('ComSpec', 'Machine') }
if (-not $commandShell) {
    # Chrome uses COMSPEC even for .exe hosts unless direct launch is enabled.
    $commandShell = Join-Path ([Environment]::SystemDirectory) 'cmd.exe'
    if (-not (Test-Path -LiteralPath $commandShell)) { throw 'Windows cmd.exe is missing.' }
    [Environment]::SetEnvironmentVariable('ComSpec', $commandShell, 'User')
    Write-Host 'Restored the Windows ComSpec user variable. Completely exit Chrome and reopen it to apply this change.'
}
if (-not (Test-Path -LiteralPath $commandShell)) { throw 'The registered Windows ComSpec path does not exist.' }
$env:ComSpec = $commandShell

function Resolve-PhysicalPath([string] $Path) {
    # MSIX can redirect AppData writes. Chrome must receive the actual on-disk path.
    $pathJson = & $venvPython -c 'import json, pathlib, sys; print(json.dumps(str(pathlib.Path(sys.argv[1]).resolve(strict=True))))' $Path
    if ($LASTEXITCODE -ne 0) { throw "Cannot resolve the physical path: $Path" }
    return ($pathJson | ConvertFrom-Json)
}

if (-not $ExtensionId) {
    # Chromium hashes the normalized UTF-16 Windows path for unpacked extensions.
    $extensionPath = (Get-Item -LiteralPath (Join-Path $repoRoot 'extension')).FullName
    if ($extensionPath -match '^[a-z]:') { $extensionPath = $extensionPath.Substring(0, 1).ToUpperInvariant() + $extensionPath.Substring(1) }
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try { $hash = $sha256.ComputeHash([Text.Encoding]::Unicode.GetBytes($extensionPath)) }
    finally { $sha256.Dispose() }
    $hex = [BitConverter]::ToString($hash, 0, 16).Replace('-', '').ToLowerInvariant()
    $ExtensionId = -join ($hex.ToCharArray() | ForEach-Object { [char](97 + [Convert]::ToInt32([string]$_, 16)) })
}

$hostDirectory = Join-Path $env:LOCALAPPDATA 'MyAudible\native-host'
New-Item -ItemType Directory -Path $hostDirectory -Force | Out-Null
$hostDirectory = Resolve-PhysicalPath $hostDirectory
$stateDirectory = Split-Path -Parent $hostDirectory
$commandFile = Join-Path $hostDirectory 'launch.cmd'
$manifestFile = Join-Path $hostDirectory 'com.myaudible.launcher.json'
$nativeScript = Join-Path $PSScriptRoot 'native_host.py'
# Prevent cmd.exe expansion of unusual checkout paths; quoted paths protect spaces and &.
if (($venvPython + $nativeScript + $stateDirectory) -match '[%\r\n]') { throw 'The checkout and state paths must not contain percent signs or line breaks.' }
$command = "@echo off`r`nchcp 65001 >nul`r`nsetlocal DisableDelayedExpansion`r`nset `"MYAUDIBLE_NATIVE_MODEL=$Model`"`r`nset `"MYAUDIBLE_STATE_DIR=$stateDirectory`"`r`n`"$venvPython`" -u `"$nativeScript`" %*`r`n"
[IO.File]::WriteAllText($commandFile, $command, [Text.UTF8Encoding]::new($false))
$manifest = @{
    name = 'com.myaudible.launcher'
    description = 'Start the local MyAudible servers'
    path = $commandFile
    type = 'stdio'
    allowed_origins = @("chrome-extension://$ExtensionId/")
} | ConvertTo-Json
[IO.File]::WriteAllText($manifestFile, $manifest, [Text.UTF8Encoding]::new($false))
$registryKey = 'HKCU:\Software\Google\Chrome\NativeMessagingHosts\com.myaudible.launcher'
New-Item -Path $registryKey -Force | Out-Null
Set-Item -LiteralPath $registryKey -Value $manifestFile
Write-Host "Registered MyAudible launcher for extension $ExtensionId."
Write-Host 'Reload MyAudible at chrome://extensions, then click the server start button in the side panel.'
