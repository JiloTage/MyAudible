<# Install the local backend and the reviewed speech.cpp Windows release. #>
[CmdletBinding()]
param(
    [string] $PythonCommand = 'py',
    [string] $PythonVersion = '3.11',
    [string] $TorchIndexUrl = 'https://download.pytorch.org/whl/cpu',
    [string] $TorchVersion = '2.6.0',
    [string] $TorchVisionVersion = '0.21.0',
    [ValidateSet('irodori-tts-mf', 'irodori-tts-mf:q8_0', 'irodori-tts', 'irodori-tts:q8_0')]
    [string] $Model = 'irodori-tts-mf'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvDirectory = Join-Path $repoRoot '.venv'
$venvPython = Join-Path $venvDirectory 'Scripts\python.exe'
$speechVersion = '0.8.4'
$installerSha256 = '5fe606f8dd49e6faf8e5a00b1d08ee620a2992e3b3f0e5d59539584820588c09'
$installerUrl = "https://raw.githubusercontent.com/nyosegawa/speech.cpp/v$speechVersion/install.ps1"

function Invoke-Checked([string] $Executable, [string[]] $Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed (exit $LASTEXITCODE): $Executable"
    }
}

if ($env:OS -ne 'Windows_NT') { throw 'Run this setup script on Windows x64.' }
if ($TorchIndexUrl -notmatch '^https://download\.pytorch\.org/whl/[a-z0-9]+/?$') {
    throw 'TorchIndexUrl must be an official https://download.pytorch.org/whl/<flavor> index.'
}
if ($TorchVersion -notmatch '^\d+\.\d+\.\d+$' -or $TorchVisionVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw 'Use explicit three-part TorchVersion and TorchVisionVersion values.'
}
Get-Command $PythonCommand -ErrorAction Stop | Out-Null
if (-not (Test-Path -LiteralPath $venvPython)) {
    Write-Host 'Creating the project Python environment.'
    $pythonArguments = @()
    if ($PythonCommand -eq 'py' -or $PythonCommand -eq 'py.exe') { $pythonArguments += "-$PythonVersion" }
    $pythonArguments += @('-m', 'venv', $venvDirectory)
    Invoke-Checked $PythonCommand $pythonArguments
}
Invoke-Checked $venvPython @('-c', 'import sys; assert (3, 11) <= sys.version_info[:2] <= (3, 13)')
Invoke-Checked $venvPython @('-m', 'pip', 'install', '--upgrade', 'pip')
Write-Host 'Installing PyTorch and the Yomitoku backend. Downloads may take several minutes.'
Invoke-Checked $venvPython @('-m', 'pip', 'install', "torch==$TorchVersion", "torchvision==$TorchVisionVersion", '--index-url', $TorchIndexUrl)
Invoke-Checked $venvPython @('-m', 'pip', 'install', '-e', ((Join-Path $repoRoot 'backend') + '[ocr]'))
Invoke-Checked $venvPython @('-m', 'pip', 'check')

$installerFile = Join-Path ([IO.Path]::GetTempPath()) ('myaudible-speech-' + [guid]::NewGuid().ToString('N') + '.ps1')
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Write-Host "Installing speech.cpp $speechVersion (Windows Vulkan)."
    Invoke-WebRequest -UseBasicParsing -Uri $installerUrl -OutFile $installerFile
    $downloadSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerFile).Hash.ToLowerInvariant()
    if ($downloadSha256 -ne $installerSha256) { throw 'The pinned speech.cpp installer checksum does not match; installation stopped.' }
    # The reviewed upstream installer verifies the release archive's SHA-256 too.
    & $installerFile -Version $speechVersion -NoModifyPath
} finally {
    Remove-Item -LiteralPath $installerFile -Force -ErrorAction SilentlyContinue
}
$speechExecutable = Join-Path $env:LOCALAPPDATA 'Programs\speech.cpp\current\speech.exe'
Invoke-Checked $speechExecutable @('--version')
Invoke-Checked $speechExecutable @('devices')
Write-Host "Downloading the pinned $Model model (about 1.92 GB for the default F16 model)."
Invoke-Checked $speechExecutable @('pull', $Model)
& (Join-Path $PSScriptRoot 'install-native-host.ps1') -Model $Model
Write-Host 'Setup complete. Reload the extension and start MyAudible from its side panel, or use scripts\run-windows.ps1.'
