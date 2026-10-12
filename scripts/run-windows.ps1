<# Run the local speech server and backend until Ctrl+C. #>
[CmdletBinding()]
param(
    [ValidateSet('auto', 'gpu', 'cpu')]
    [string] $SpeechDevice = 'gpu',
    [ValidateSet('auto', 'cpu', 'cuda')]
    [string] $OcrDevice = 'auto',
    [ValidateSet('irodori-tts-mf', 'irodori-tts-mf:q8_0', 'irodori-tts', 'irodori-tts:q8_0')]
    [string] $Model = 'irodori-tts-mf',
    [string] $SpeechExecutable = '',
    [ValidateRange(1, 65535)]
    [int] $SpeechPort = 8081,
    [ValidateRange(1024, 65535)]
    [int] $BackendPort = 8765
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPython)) { throw 'Run scripts\setup-windows.ps1 first.' }
if (-not $SpeechExecutable) {
    $SpeechExecutable = Join-Path $env:LOCALAPPDATA 'Programs\speech.cpp\current\speech.exe'
    if (-not (Test-Path -LiteralPath $SpeechExecutable)) {
        $SpeechExecutable = (Get-Command speech -ErrorAction Stop).Source
    }
}
& $venvPython (Join-Path $PSScriptRoot 'run-local.py') --speech $SpeechExecutable --model $Model --speech-device $SpeechDevice --ocr-device $OcrDevice --speech-port $SpeechPort --backend-port $BackendPort
if ($LASTEXITCODE -ne 0) { throw "MyAudible exited with code $LASTEXITCODE." }
