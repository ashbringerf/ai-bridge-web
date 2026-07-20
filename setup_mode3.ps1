# AI Bridge - Mode 3 setup (web drives YOUR local opencode; model via admin's internal mify)
# Run in PowerShell:
#   irm https://ashbringerf.github.io/ai-bridge-web/setup_mode3.ps1 | iex
#
# What it does:
#   1) downloads bridge.py (role=local) + relay client
#   2) writes .env for both
#   3) generates opencode.json so opencode's model goes through local relay (127.0.0.1:8799)
#   4) starts BOTH: relay (model bridge) + bridge.py (listens web commands, runs opencode locally)
#
# Result: from the web page (mode "跑在我自己电脑, 模型借管理员mify") you send a task,
#         it runs on THIS PC editing your project, model borrows admin's mify. All via GitHub, no tunnel.

$ErrorActionPreference = "Stop"
$RAW = "https://ashbringerf.github.io/ai-bridge-web"
$DIR = Join-Path $env:USERPROFILE ".ai-bridge-mode3"
$utf8 = New-Object System.Text.UTF8Encoding $false

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " AI Bridge - Mode 3 (web -> your local opencode, model via admin mify)" -ForegroundColor Cyan
Write-Host "============================================================`n"

# 1. python
$pyDetected = (Get-Command python -ErrorAction SilentlyContinue).Source
if ($pyDetected) {
  $ans = Read-Host "python path [$pyDetected] (Enter=use it, or paste another)"
  $py = if ($ans) { $ans } else { $pyDetected }
} else {
  $py = Read-Host "python.exe path (install from python.org if none)"
}
Write-Host "  python = $py"
& $py -m pip install -q requests 2>$null

# 1b. opencode
$ocDetected = (Get-Command opencode -ErrorAction SilentlyContinue).Source
if ($ocDetected) {
  $ans = Read-Host "opencode path [$ocDetected] (Enter=use it, or paste another)"
  $ocBin = if ($ans) { $ans } else { $ocDetected }
} else {
  Write-Host "  opencode not found." -ForegroundColor Yellow
  $ans = Read-Host "paste opencode path, or Enter to auto-install"
  if ($ans) {
    $ocBin = $ans
  } else {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
      Write-Host "  npm not found (Node.js missing)." -ForegroundColor Yellow
      if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "  installing Node.js via winget (accept prompts)..." -ForegroundColor Cyan
        winget install -e --id OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
        Write-Host "  Node.js installed. CLOSE this window, open a NEW PowerShell, rerun this script." -ForegroundColor Green
        pause; exit
      } else {
        Write-Host "  Please install Node.js manually: https://nodejs.org (LTS), then rerun." -ForegroundColor Red
        pause; exit
      }
    }
    npm install -g opencode-ai | Out-Null
    $ocBin = (Get-Command opencode -ErrorAction SilentlyContinue).Source
  }
}
Write-Host "  opencode = $ocBin"

# 2. download bridge.py + relay client
New-Item -ItemType Directory -Path $DIR -Force | Out-Null
Invoke-WebRequest "$RAW/bridge.py"            -OutFile (Join-Path $DIR "bridge.py") -UseBasicParsing
Invoke-WebRequest "$RAW/mify_relay_client.py" -OutFile (Join-Path $DIR "relay.py")  -UseBasicParsing

# 3. account + mailbox (same repo as admin; you get token from admin)
$owner = Read-Host "admin GitHub owner [ashbringerf]"; if (-not $owner) { $owner = "ashbringerf" }
$repo  = Read-Host "mailbox repo [opencode-bridge]"; if (-not $repo) { $repo = "opencode-bridge" }
$token = Read-Host "GitHub token (from admin)"
$user  = Read-Host "your username (must match the one you use on the web page, e.g. fengtian)"
$pass  = Read-Host "your access password (from admin)"

# 4. project dir (where opencode runs/edits on THIS pc)
$proj = Read-Host "project dir for agent [current: $(Get-Location)] (Enter=current, or paste path)"
if ($proj) { if (-not (Test-Path $proj)) { New-Item -ItemType Directory -Path $proj -Force | Out-Null } }
else { $proj = (Get-Location).Path }
Write-Host "  project dir = $proj"

$model = Read-Host "model to use [ppio/pa/gpt-5.5]"; if (-not $model) { $model = "ppio/pa/gpt-5.5" }

# 5. write shared .env (used by BOTH bridge.py and relay.py)
#    bridge.py role=local: only claims to_pc_local/<user>, does NOT touch mify_req.
#    the user is treated as owner of this local bridge so its token check passes,
#    and allow-list limits it to this user only.
$envText = @"
BRIDGE_GH_TOKEN=$token
BRIDGE_REPO_OWNER=$owner
BRIDGE_REPO_NAME=$repo
BRIDGE_BRANCH=main
BRIDGE_ROLE=local
BRIDGE_LOCAL_USER=$user
BRIDGE_OWNER_USER=$user
BRIDGE_ALLOW_USERS=$user
BRIDGE_WORKDIR=$proj
BRIDGE_OPENCODE_BIN=$ocBin
RELAY_PORT=8799
"@
[System.IO.File]::WriteAllText((Join-Path $DIR ".env"), $envText, $utf8)

# 6. generate opencode.json in the project dir -> model goes through local relay
$oc = @{
  '$schema' = "https://opencode.ai/config.json"
  provider = @{ viamify = @{ npm = "@ai-sdk/openai-compatible"; name = "ViaMify"; options = @{ baseURL = "http://127.0.0.1:8799/v1"; apiKey = "relay-placeholder" }; models = @{ "$model" = @{ name = "$model via mify" } } } }
  model = "viamify/$model"
}
$ocPath = Join-Path $proj "opencode.json"
[System.IO.File]::WriteAllText($ocPath, ($oc | ConvertTo-Json -Depth 6), $utf8)
Write-Host "  wrote $ocPath (model via local relay)" -ForegroundColor Green

# 7. start relay (model bridge) in a new window, keep running
Write-Host "`n=== starting relay (model bridge) ===" -ForegroundColor Green
Start-Process powershell -ArgumentList "-NoProfile","-NoExit","-Command","Set-Location '$DIR'; & '$py' relay.py"

Start-Sleep -Seconds 2

# 8. start bridge.py (role=local) in THIS window, keep it open
Write-Host "=== starting local bridge (listens web commands -> runs opencode here) ===" -ForegroundColor Green
Write-Host "KEEP BOTH WINDOWS OPEN. Now go to the web page, choose mode 'my PC via admin mify', and send a task.`n" -ForegroundColor Yellow
Set-Location $DIR
& $py bridge.py
