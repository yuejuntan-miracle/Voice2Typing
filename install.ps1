# V2T one-line installer for Windows (PowerShell).
#
#   powershell -ExecutionPolicy Bypass -c "iwr -useb https://raw.githubusercontent.com/yuejuntan-miracle/V2T/main/install.ps1 | iex"
#
# It clones (or updates) the repo, creates a virtual environment, installs the
# Python dependencies, then asks for your Gemini API key and a couple of
# settings and writes config.json. Run it again any time to update.
$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/yuejuntan-miracle/V2T.git"
$Dir = if ($env:V2T_DIR) { $env:V2T_DIR } else { Join-Path (Get-Location) "V2T" }

Write-Host "============================================================"
Write-Host " V2T installer (built on Gemini by YJ)"
Write-Host "============================================================"

# -- locate Python 3.10+ ---------------------------------------------------
function Find-Python {
    foreach ($name in @("python", "python3", "py")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        try {
            $ver = & $cmd.Source --version 2>&1
        } catch { continue }
        if ($ver -match "(\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 10) { return $cmd.Source }
        }
    }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Host "Python 3.10+ not found; trying to install it..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements
        } catch { Write-Host "(winget install failed)" }
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        $Python = Find-Python
    }
}
if (-not $Python) {
    Write-Host "Python 3.10+ is required but could not be installed automatically."
    Write-Host "  Install Python 3.12: https://www.python.org/downloads/"
    Write-Host "  (tick 'Add python.exe to PATH' during installation)"
    exit 1
}
Write-Host ("Using " + (& $Python --version 2>&1) + " at " + $Python)

# -- get the code ----------------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "git is required but was not found. Install Git and re-run:"
    Write-Host "  https://git-scm.com/download/win"
    exit 1
}
if (Test-Path (Join-Path $Dir ".git")) {
    Write-Host "Updating existing checkout in $Dir ..."
    git -C $Dir pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Host "(could not pull; using the current copy)" }
} else {
    Write-Host "Cloning V2T into $Dir ..."
    git clone --depth 1 $RepoUrl $Dir
    if ($LASTEXITCODE -ne 0) { Write-Host "Clone failed."; exit 1 }
}
Set-Location $Dir

# -- virtual environment + Python deps ------------------------------------
$VenvPy = Join-Path $Dir ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Write-Host "Creating virtual environment ..."
    & $Python -m venv .venv
}
& $VenvPy -m pip install --upgrade pip
& $VenvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Host "Dependency installation failed."; exit 1 }

# -- configuration ---------------------------------------------------------
Write-Host ""
Write-Host "Let's set up config.json (press Enter to accept the default)."
$ApiKey = Read-Host "Gemini API key (required)"
while ([string]::IsNullOrWhiteSpace($ApiKey)) {
    Write-Host "The Gemini API key is required."
    $ApiKey = Read-Host "Gemini API key (required)"
}
$Language = Read-Host "Language [Auto]"
if ([string]::IsNullOrWhiteSpace($Language)) { $Language = "Auto" }
$Trigger = Read-Host "Push-to-talk shortcut [mouse:x2]"
if ([string]::IsNullOrWhiteSpace($Trigger)) { $Trigger = "mouse:x2" }
$Fix = Read-Host "Enable the English fixer (Gemma/Gemini)? [Y/n]"
$Rewrite = -not ($Fix -match '^[nN]')

$env:CONFIG_API_KEY = $ApiKey.Trim()
$env:CONFIG_LANG = $Language.Trim()
$env:CONFIG_TRIGGER = $Trigger.Trim()
$env:CONFIG_REWRITE = if ($Rewrite) { "true" } else { "false" }
@'
import json, os
path = "config.json"
cfg = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        cfg = {}
cfg["gemini_api_key"] = os.environ["CONFIG_API_KEY"].strip()
cfg["language"] = os.environ["CONFIG_LANG"].strip() or "Auto"
cfg["trigger_shortcut"] = os.environ["CONFIG_TRIGGER"].strip() or "mouse:x2"
cfg["rewrite_enabled"] = os.environ["CONFIG_REWRITE"] == "true"
with open(path, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
print("Wrote", path)
'@ | & $VenvPy -

# -- done ------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================"
Write-Host " Setup complete."
Write-Host ("   Start V2T:   cd `"$Dir`" ; .\windows\run_dictate.bat")
Write-Host "============================================================"