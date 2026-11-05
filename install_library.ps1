$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$libraryPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $libraryPython)) {
    throw "Run install.ps1 first to install the LUX runtime."
}
& $libraryPython -m pip install -r requirements-library.txt
if ($LASTEXITCODE -ne 0) { throw "PDF dependency installation failed. Check the network and retry." }
Write-Host "PDF library is ready. Restart LUX and refresh the browser." -ForegroundColor Green
