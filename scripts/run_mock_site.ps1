<# Start the bundled MOCK trading website on http://127.0.0.1:8765 (for adapter development/tests). #>
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
& .\.venv\Scripts\python.exe -m mwbot.mocksite @args
