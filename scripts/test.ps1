<# Run every check the CI runs (add -SkipBrowser to skip Playwright tests). #>
param([switch]$SkipBrowser)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
$py = ".\.venv\Scripts\python.exe"
& $py -m ruff check backend
& $py -m ruff format --check backend
Push-Location backend
& $py -m mypy mwbot
if ($SkipBrowser) { & $py -m pytest -m "not browser" -p no:warnings } else { & $py -m pytest -p no:warnings }
Pop-Location
Push-Location frontend
npm run typecheck
npm test
npm run build
Pop-Location
