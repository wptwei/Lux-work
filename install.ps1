$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3.11 or 3.12 was not found. Install Python and enable Add Python to PATH."
}

if (-not (Test-Path -LiteralPath ".venv")) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create the Python virtual environment." }
}

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "The virtual environment Python was not created. Check your Python installation and folder permissions."
}

& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade pip." }

& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed. Check the network and the pip output above." }

if (-not (Test-Path -LiteralPath ".env") -and (Test-Path -LiteralPath ".env.example")) {
    Copy-Item -LiteralPath ".env.example" -Destination ".env"
}

& $venvPython -m unittest discover -v
if ($LASTEXITCODE -ne 0) { throw "Automated tests failed. Review the test output above." }

Write-Host "Installation complete. Review .env, then run .\healthcheck.ps1 and .\start_web.ps1." -ForegroundColor Green
