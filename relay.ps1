# AI Bridge - one-line relay setup (use YOUR local opencode + admin's internal mify)
# Run in PowerShell:
#   irm https://ashbringerf.github.io/ai-bridge-web/relay.ps1 | iex
#
# What it does: downloads relay -> asks token -> configs opencode to use mify via relay -> starts relay.
# Your opencode runs locally on THIS PC (edits your files); the MODEL goes through admin's mify.

$ErrorActionPreference = "Stop"
$RAW = "https://ashbringerf.github.io/ai-bridge-web"
$DIR = Join-Path $env:USERPROFILE ".ai-relay"

Write-Host "=== AI Bridge relay setup ===" -ForegroundColor Cyan
Write-Host "Use this PC's opencode, model via admin's internal mify.`n"

# 1. python
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = Read-Host "python.exe path (install python.org first if none)" }
& $py -m pip install -q requests 2>$null

# 2. download relay
New-Item -ItemType Directory -Path $DIR -Force | Out-Null
Invoke-WebRequest "$RAW/mify_relay_client.py" -OutFile (Join-Path $DIR "relay.py") -UseBasicParsing

# 3. token (admin gives you a token that can read/write the opencode-bridge mailbox)
$owner = Read-Host "admin GitHub owner [ashbringerf]"; if (-not $owner) { $owner = "ashbringerf" }
$repo  = Read-Host "mailbox repo [opencode-bridge]"; if (-not $repo) { $repo = "opencode-bridge" }
$token = Read-Host "GitHub token (from admin)"
$env_ = "BRIDGE_GH_TOKEN=$token`nBRIDGE_REPO_OWNER=$owner`nBRIDGE_REPO_NAME=$repo`nBRIDGE_BRANCH=main`nRELAY_PORT=8799`n"
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText((Join-Path $DIR ".env"), $env_, $utf8)

# 4. auto-config opencode to use relay as model provider (project-level in current dir)
$model = Read-Host "model to use [ppio/pa/gpt-5.5]"; if (-not $model) { $model = "ppio/pa/gpt-5.5" }
$oc = @{
  '$schema' = "https://opencode.ai/config.json"
  provider = @{ viamify = @{ npm = "@ai-sdk/openai-compatible"; name = "ViaMify"; options = @{ baseURL = "http://127.0.0.1:8799/v1" }; models = @{ "$model" = @{ name = "$model via mify" } } } }
  model = "viamify/$model"
}
$ocPath = Join-Path (Get-Location) "opencode.json"
[System.IO.File]::WriteAllText($ocPath, ($oc | ConvertTo-Json -Depth 6), $utf8)
Write-Host "wrote opencode.json in current dir -> uses mify via relay" -ForegroundColor Green

# 5. start relay
Write-Host "`n=== Done! relay starting. Keep this window open. ===" -ForegroundColor Green
Write-Host "In ANOTHER terminal, cd to your project and run: opencode run `"your task`"" -ForegroundColor Green
Write-Host "opencode runs here, model via admin mify.`n"
Set-Location $DIR
& $py (Join-Path $DIR "relay.py")
