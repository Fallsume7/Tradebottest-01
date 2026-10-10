<#
  Start a DEDICATED Chrome window for the bot (not your everyday profile) and open your MarketWatch game.
  YOU log in to MarketWatch in that window, yourself. The bot then attaches to this window over a local-only
  debugging port; it never sees or stores your password. Leave the window open (minimised is fine).

  Usage:  powershell -ExecutionPolicy Bypass -File scripts\marketwatch_chrome.ps1 [-DryRun]
  Needs MWBOT_MARKETWATCH_GAME_URL in .env (optional: MWBOT_MARKETWATCH_CDP_URL, default http://127.0.0.1:9222).
#>
param([switch]$DryRun)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

function Get-EnvValue([string]$name, [string]$default = "") {
  if (Test-Path ".env") {
    $line = Get-Content ".env" | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -Last 1
    if ($line) { return ($line -split "=", 2)[1].Trim().Trim('"').Trim("'") }
  }
  return $default
}

$gameUrl = Get-EnvValue "MWBOT_MARKETWATCH_GAME_URL" "https://www.marketwatch.com/games"
$cdp     = Get-EnvValue "MWBOT_MARKETWATCH_CDP_URL" "http://127.0.0.1:9222"
$uri     = [Uri]$cdp
if ($uri.Host -notin @("127.0.0.1", "localhost", "::1")) { throw "MWBOT_MARKETWATCH_CDP_URL must be on this computer" }
if (([Uri]$gameUrl).Host -notmatch "(^|\.)marketwatch\.com$") { throw "MWBOT_MARKETWATCH_GAME_URL must be a marketwatch.com address" }

$candidates = @(
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
  "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$chrome = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $chrome) { throw "Chrome not found. Install it (winget install -e --id Google.Chrome) or edit this script." }

$profile = Join-Path (Get-Location) "chrome_profile"   # dedicated, git-ignored; holds your login for the bot's window
$chromeArgs = @(
  "--remote-debugging-port=$($uri.Port)",
  "--remote-debugging-address=127.0.0.1",
  "--user-data-dir=`"$profile`"",
  "--no-first-run",
  "--new-window",
  $gameUrl
)

$listening = $false
try { $null = Invoke-RestMethod "$cdp/json/version" -TimeoutSec 2; $listening = $true } catch {}
if ($listening) { Write-Host "The bot's Chrome is already running at $cdp - nothing to do."; return }

Write-Host "Chrome:   $chrome"
Write-Host "Profile:  $profile  (dedicated; not your everyday profile)"
Write-Host "Opening:  $gameUrl"
if ($DryRun) { Write-Host "DryRun: would run: chrome $($chromeArgs -join ' ')"; return }
Start-Process -FilePath $chrome -ArgumentList $chromeArgs
Write-Host ""
Write-Host "Now log in to MarketWatch in that window yourself (solve any CAPTCHA there). Then leave it open."
