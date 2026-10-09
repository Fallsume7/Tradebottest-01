<#
  One-time setup on Windows (PowerShell 5.1+ or 7).
  Usage:  powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
  Requires: Python 3.12+ (py launcher), Node.js 20+/22 LTS, Git. Chrome/Chromium is installed by Playwright.
#>
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

Write-Host "==> Creating virtual environment (.venv)"
if (-not (Test-Path ".venv")) { py -3.12 -m venv .venv }
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
Write-Host "==> Installing backend (editable) with dev + browser tooling"
& .\.venv\Scripts\python.exe -m pip install -e "backend[dev]"
Write-Host "==> Installing Playwright Chromium (used by the mock-site tests; your own Chrome is not touched)"
& .\.venv\Scripts\python.exe -m playwright install chromium

Write-Host "==> Installing + building the dashboard"
Push-Location frontend
npm ci
npm run build
Pop-Location

if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "==> Created .env from .env.example (demo mode works with no keys; never commit .env)"
}
New-Item -ItemType Directory -Force data | Out-Null
Write-Host ""
Write-Host "Done. Start the app with:  powershell -File scripts\run.ps1   then open http://127.0.0.1:8000"
