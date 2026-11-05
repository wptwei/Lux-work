param([string]$Model = "small")
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$voicePython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $voicePython)) {
    throw "Run install.ps1 first to install the LUX runtime."
}
& $voicePython -m pip install -r requirements-voice.txt
if ($LASTEXITCODE -ne 0) { throw "Voice dependency installation failed." }
$env:LUX_ASR_MODEL = $Model
Write-Host "Preparing local speech model '$Model'. This step downloads public model files."
& $voicePython .\voice_asr.py --prepare
if ($LASTEXITCODE -ne 0) { throw "Voice model preparation failed. Check the network and retry." }
if ($Model -ne "small") {
    Write-Host "Set LUX_ASR_MODEL=$Model in .env before restarting LUX."
}
Write-Host "Voice input is ready. Restart LUX and refresh the browser." -ForegroundColor Green
