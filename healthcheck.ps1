$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not $env:LUX_ENV_FILE) { $envFile = Join-Path $PSScriptRoot ".env" } else { $envFile = $env:LUX_ENV_FILE }

if (Test-Path $envFile) {
    Get-Content $envFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $name, $value = $line.Split("=", 2)
            [Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), "Process")
        }
    }
}

& ".\.venv\Scripts\python.exe" healthcheck.py
