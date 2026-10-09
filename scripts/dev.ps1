<# Development mode: backend on :8000 and Vite dev server (hot reload) on :5173. #>
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
Start-Process -FilePath ".\.venv\Scripts\mwbot.exe" -ArgumentList "serve"
Set-Location frontend
npm run dev
