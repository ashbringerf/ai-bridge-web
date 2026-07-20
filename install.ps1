# AI Bridge - self mode installer (Windows PowerShell)
# Usage (run one line in PowerShell):
#   irm https://ashbringerf.github.io/ai-bridge-web/install.ps1 | iex
#
# It downloads bridge.py, asks for repo/token/workdir/paths, writes .env, starts bridge.
# Lets you control THIS PC's local opencode from the web (self mode), editing your chosen project dir.

$ErrorActionPreference = "Stop"
$RAW = "https://ashbringerf.github.io/ai-bridge-web"
$HOMEDIR = Join-Path $env:USERPROFILE ".ai-bridge"

function Has($cmd){ [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
function SrcOf($cmd){ $g=Get-Command $cmd -ErrorAction SilentlyContinue; if($g){$g.Source}else{""} }

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  AI Bridge - self mode installer" -ForegroundColor Cyan
Write-Host "  (control THIS PC's opencode from the web + your project dir)" -ForegroundColor Cyan
Write-Host "============================================================`n"

# ---- 1. Python: auto-detect or let user specify ----
Write-Host "[1/6] Python..." -ForegroundColor Yellow
$pyPath = SrcOf "python"
if ($pyPath) {
  Write-Host "  detected python: $pyPath"
  $ans = Read-Host "  use this python? (y / or paste another python.exe path) [y]"
  if ($ans -and $ans -ne "y") { $pyPath = $ans }
} else {
  Write-Host "  python not found in PATH." -ForegroundColor Red
  $pyPath = Read-Host "  paste your python.exe absolute path (or install from python.org then rerun)"
}
if (-not (Test-Path $pyPath) -and -not (Has $pyPath)) { Write-Host "  [X] python not usable: $pyPath" -ForegroundColor Red; return }
Write-Host "  [OK] python = $pyPath"

# ---- 2. agent: use existing (specify path) or auto-install ----
Write-Host "`n[2/6] AI agent (opencode/claude/codex)..." -ForegroundColor Yellow
& $pyPath -m pip install -q requests pillow 2>$null
$found = @{}
foreach($c in "opencode","claude","codex"){ $s=SrcOf $c; if($s){ $found[$c]=$s } }
$customBin = ""
if ($found.Count -gt 0) {
  Write-Host "  detected:" -ForegroundColor Green
  $found.GetEnumerator() | ForEach-Object { Write-Host "    $($_.Key) -> $($_.Value)" }
  $ans = Read-Host "  use existing? (y / n=reinstall opencode) [y]"
  if ($ans -eq "n") { npm install -g opencode-ai | Out-Null }
} else {
  if (Has "npm") { Write-Host "  none found, installing opencode..."; npm install -g opencode-ai | Out-Null }
  else { Write-Host "  no agent & no npm. install Node.js + opencode, or specify a path below." -ForegroundColor Red }
}
$customBin = Read-Host "  specify agent exe absolute path? (blank = auto-detect from PATH)"

# ---- 3. download bridge.py ----
Write-Host "`n[3/6] downloading bridge..." -ForegroundColor Yellow
New-Item -ItemType Directory -Path $HOMEDIR -Force | Out-Null
Invoke-WebRequest "$RAW/bridge.py" -OutFile (Join-Path $HOMEDIR "bridge.py") -UseBasicParsing
Write-Host "  saved to $HOMEDIR\bridge.py"

# ---- 4. config ----
Write-Host "`n[4/6] config (your own mailbox repo + project dir)" -ForegroundColor Yellow
$owner  = Read-Host "  your GitHub username (owner)"
$repo   = Read-Host "  your mailbox repo name (create a private repo, e.g. my-bridge)"
$token  = Read-Host "  GitHub token (Contents read/write on that repo)"
$user   = Read-Host "  login username (what you type on the web)"
$workdir= Read-Host "  project dir the agent works in (abs path, e.g. D:\myproject)"

# ---- 5. write .env ----
Write-Host "`n[5/6] writing config..." -ForegroundColor Yellow
$envtext = "BRIDGE_GH_TOKEN=$token`nBRIDGE_REPO_OWNER=$owner`nBRIDGE_REPO_NAME=$repo`nBRIDGE_BRANCH=main`nBRIDGE_OWNER_USER=$user`nBRIDGE_ALLOW_USERS=$user`nBRIDGE_WORKDIR=$workdir`n"
if ($customBin -and $customBin.Trim() -ne "") {
  if ($customBin -match "claude") { $envtext += "BRIDGE_CLAUDE_BIN=$customBin`n" }
  elseif ($customBin -match "codex") { $envtext += "BRIDGE_CODEX_BIN=$customBin`n" }
  else { $envtext += "BRIDGE_OPENCODE_BIN=$customBin`n" }
}
$utf8 = New-Object System.Text.UTF8Encoding $false
[System.IO.File]::WriteAllText((Join-Path $HOMEDIR ".env"), $envtext, $utf8)
Write-Host "  wrote $HOMEDIR\.env"

# ---- 6. start ----
Write-Host "`n============================================================" -ForegroundColor Green
Write-Host "  Done! Now:" -ForegroundColor Green
Write-Host "  1) keep this window open (bridge runs here)" -ForegroundColor Green
Write-Host "  2) on the web, pick 'run on my own PC', owner/repo = $owner/$repo" -ForegroundColor Green
Write-Host "  3) username = $user (self mode needs no passcode)" -ForegroundColor Green
Write-Host "  agent runs on THIS PC, edits: $workdir" -ForegroundColor Green
Write-Host "============================================================`n"
Set-Location $HOMEDIR
& $pyPath (Join-Path $HOMEDIR "bridge.py")
