<# Start the API + dashboard (serves frontend\dist). Ctrl+C to stop. #>
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path "frontend\dist\index.html")) { Write-Warning "frontend\dist missing - run scripts\setup.ps1 (or: cd frontend; npm run build)" }
& .\.venv\Scripts\mwbot.exe serve @args
