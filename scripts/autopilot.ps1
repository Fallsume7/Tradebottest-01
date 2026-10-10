<#
  One command to run the bot hands-off: make sure the bot's Chrome window is up, then start the app with
  autopilot on (it follows the real trading calendar by itself, from the morning scan to the end-of-day exit).

  Usage:  powershell -ExecutionPolicy Bypass -File scripts\autopilot.ps1
  Stop:   Ctrl+C in this window (open positions are NOT sold by stopping; use the dashboard's E-STOP first).
  This does not install anything to start automatically; that is a separate, later decision.
#>
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path ".env")) { throw ".env missing - run scripts\setup.ps1 first" }
$mode = (Get-Content ".env" | Where-Object { $_ -match "^\s*MWBOT_EXECUTION_MODE\s*=" } | Select-Object -Last 1)
if ($mode -match "marketwatch") {
  & powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\marketwatch_chrome.ps1"
  Start-Sleep -Seconds 3
} else {
  Write-Warning "MWBOT_EXECUTION_MODE is not 'marketwatch': autopilot will run the bot on its own virtual account only."
}
$env:MWBOT_AUTOPILOT = "true"
& .\.venv\Scripts\mwbot.exe serve @args
