# Voice Dictation - System Hardware & Environment Checker
[CmdletBinding()]
param(
    [Parameter(Mandatory=$false)]
    [ValidateSet("check", "find-python", "install-python", "ensure-vcredist")]
    [string]$Action = "check"
)

$ErrorActionPreference = "SilentlyContinue"

function Write-Header($text) {
    Write-Host ""
    Write-Host ("=" * 64) -ForegroundColor Cyan
    Write-Host " $text" -ForegroundColor Cyan
    Write-Host ("=" * 64) -ForegroundColor Cyan
    Write-Host ""
}

function Find-PythonExecutable {
    # 1. Try py launcher with 3.12
    try {
        $exe = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null)
        if ($exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
    } catch {}

    # 2. Try py launcher default
    try {
        $exe = (& py -c "import sys; print(sys.executable)" 2>$null)
        if ($exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
    } catch {}

    # 3. Try python in PATH
    try {
        $exe = (& python -c "import sys; print(sys.executable)" 2>$null)
        if ($exe -and (Test-Path $exe.Trim())) {
            $ver = (& python -c "import sys; print(sys.version_info[:2] >= (3, 10))" 2>$null)
            if ($ver -and $ver.Trim() -eq "True") {
                return $exe.Trim()
            }
        }
    } catch {}

    # 4. Try common installation directories
    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
        (Join-Path $env:ProgramFiles "Python312\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
        (Join-Path $env:ProgramFiles "Python311\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python310\python.exe"),
        (Join-Path $env:ProgramFiles "Python310\python.exe")
    )
    foreach ($cand in $candidates) {
        if (Test-Path $cand) { return $cand }
    }
    return $null
}

function Install-Python312 {
    Write-Host "[*] Installing Python 3.12 (64-bit)..." -ForegroundColor Yellow
    $hasWinget = $null -ne (Get-Command winget -ErrorAction SilentlyContinue)

    if ($hasWinget) {
        Write-Host "    Attempting installation via Windows Package Manager (winget)..." -ForegroundColor Gray
        $proc = Start-Process winget -ArgumentList "install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements" -NoNewWindow -PassThru -Wait
        $found = Find-PythonExecutable
        if ($found) {
            Write-Host "    [OK] Python installed successfully: $found" -ForegroundColor Green
            return $found
        }
    }

    # Fallback to direct python.org installer
    Write-Host "    Downloading official Python 3.12.9 installer from python.org..." -ForegroundColor Gray
    $installer = Join-Path $env:TEMP "python-3.12.9-amd64.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe" -OutFile $installer
        Write-Host "    Running installer silently (adding to PATH)..." -ForegroundColor Gray
        $proc = Start-Process -FilePath $installer -ArgumentList "/passive InstallAllUsers=0 PrependPath=1 Include_pip=1" -Wait -PassThru
        Start-Sleep -Seconds 2
    } catch {
        Write-Host "    [!] Could not download Python automatically: $_" -ForegroundColor Red
    }

    $found = Find-PythonExecutable
    if ($found) {
        Write-Host "    [OK] Python installed successfully: $found" -ForegroundColor Green
        return $found
    }

    Write-Host "    [ERROR] Python installation did not complete. Please download and install from: https://www.python.org/downloads/" -ForegroundColor Red
    return $null
}

function Check-VCRedist {
    try {
        $p = Get-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64" -ErrorAction SilentlyContinue
        return ($p -and $p.Installed -eq 1)
    } catch {
        return $false
    }
}

function Install-VCRedist {
    Write-Host "[*] Installing Microsoft Visual C++ 2015-2022 Redistributable (x64)..." -ForegroundColor Yellow
    $hasWinget = $null -ne (Get-Command winget -ErrorAction SilentlyContinue)
    if ($hasWinget) {
        Write-Host "    Installing via winget..." -ForegroundColor Gray
        Start-Process winget -ArgumentList "install --id Microsoft.VCRedist.2015+.x64 -e --source winget --accept-package-agreements --accept-source-agreements" -NoNewWindow -Wait
        if (Check-VCRedist) {
            Write-Host "    [OK] Visual C++ Redistributable installed." -ForegroundColor Green
            return $true
        }
    }

    # Direct download fallback
    $installer = Join-Path $env:TEMP "vc_redist.x64.exe"
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile $installer
        Start-Process -FilePath $installer -ArgumentList "/passive /norestart" -Wait
        if (Check-VCRedist) {
            Write-Host "    [OK] Visual C++ Redistributable installed." -ForegroundColor Green
            return $true
        }
    } catch {
        Write-Host "    [!] Could not download VC++ Redistributable: $_" -ForegroundColor Red
    }
    return $false
}

function Get-SystemHardwareReport {
    $report = [PSCustomObject]@{
        OS = ""
        Is64Bit = [Environment]::Is64BitOperatingSystem
        CPU = ""
        Cores = $env:NUMBER_OF_PROCESSORS
        RAM_Total_GB = 0
        RAM_Free_GB = 0
        Disk_Free_GB = 0
        DriveLetter = ""
        GPUs = @()
        HasIntelArc = $false
        HasNvidia = $false
        NPUs = @()
        VCRedist = $false
        PythonPath = $null
    }

    # OS & Architecture
    $osInfo = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
    if ($osInfo) {
        $report.OS = "$($osInfo.Caption) ($($osInfo.OSArchitecture))"
        $report.RAM_Total_GB = [math]::Round($osInfo.TotalVisibleMemorySize / 1MB, 1)
        $report.RAM_Free_GB = [math]::Round($osInfo.FreePhysicalMemory / 1MB, 1)
    }

    # CPU
    $cpuInfo = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue
    if ($cpuInfo) {
        $report.CPU = ($cpuInfo.Name -join ", ").Trim()
    }

    # Disk Space
    try {
        $currDrive = (Get-Location).Drive.Name
        $drive = Get-PSDrive $currDrive -ErrorAction SilentlyContinue
        if ($drive) {
            $report.DriveLetter = $currDrive
            $report.Disk_Free_GB = [math]::Round($drive.Free / 1GB, 1)
        }
    } catch {}

    # GPUs
    $controllers = Get-CimInstance Win32_VideoController -ErrorAction SilentlyContinue
    foreach ($c in $controllers) {
        if ($c.Name -and $c.Name -notmatch "Virtual|Remote|Basic Display") {
            $report.GPUs += "$($c.Name) (Driver $($c.DriverVersion))"
            if ($c.Name -match "Arc|Intel\(R\) Graphics|Ultra") {
                $report.HasIntelArc = $true
            }
            if ($c.Name -match "NVIDIA|GeForce|RTX") {
                $report.HasNvidia = $true
            }
        }
    }

    # NPUs
    $pnpNpus = Get-PnpDevice -ErrorAction SilentlyContinue | Where-Object {
        $_.FriendlyName -match "AI Boost|NPU|Neural|Ryzen AI|Snapdragon" -and
        $_.FriendlyName -notmatch "Input|Touchpad|Mouse|Keyboard|Display"
    }
    foreach ($n in $pnpNpus) {
        $report.NPUs += "$($n.FriendlyName) [$($n.Status)]"
    }

    $report.VCRedist = Check-VCRedist
    $report.PythonPath = Find-PythonExecutable

    return $report
}

# ----------------------------------------------------------------- Main Dispatch --
if ($Action -eq "find-python") {
    $found = Find-PythonExecutable
    if ($found) { Write-Output $found }
    exit 0
}

if ($Action -eq "install-python") {
    $installed = Install-Python312
    if ($installed) { Write-Output $installed; exit 0 }
    exit 1
}

if ($Action -eq "ensure-vcredist") {
    if (-not (Check-VCRedist)) {
        Install-VCRedist | Out-Null
    }
    exit 0
}

# Default: Full System Check
Write-Header "Voice Dictation - System Hardware & Capability Check"

$hw = Get-SystemHardwareReport

Write-Host " Operating System    : " -NoNewline -ForegroundColor Gray
Write-Host $hw.OS -ForegroundColor White

Write-Host " Architecture        : " -NoNewline -ForegroundColor Gray
if ($hw.Is64Bit) {
    Write-Host "64-bit (Supported)" -ForegroundColor Green
} else {
    Write-Host "32-bit (UNSUPPORTED - 64-bit required)" -ForegroundColor Red
}

Write-Host " Processor (CPU)     : " -NoNewline -ForegroundColor Gray
Write-Host "$($hw.CPU) ($($hw.Cores) logical cores)" -ForegroundColor White

Write-Host " System Memory (RAM) : " -NoNewline -ForegroundColor Gray
if ($hw.RAM_Total_GB -ge 8) {
    Write-Host "$($hw.RAM_Total_GB) GB total ($($hw.RAM_Free_GB) GB free) - Plenty for voice dictation" -ForegroundColor Green
} else {
    Write-Host "$($hw.RAM_Total_GB) GB total - WARNING: Low RAM (8 GB+ recommended)" -ForegroundColor Red
}

Write-Host " Free Disk Space     : " -NoNewline -ForegroundColor Gray
if ($hw.Disk_Free_GB -ge 2) {
    Write-Host "$($hw.Disk_Free_GB) GB free on $($hw.DriveLetter): (needs ~1 GB for the virtual environment)" -ForegroundColor Green
} else {
    Write-Host "$($hw.Disk_Free_GB) GB free on $($hw.DriveLetter): - WARNING: Low disk space! (~1 GB needed)" -ForegroundColor Red
}

Write-Host ""
Write-Host " Hardware Accelerators:" -ForegroundColor Cyan

if ($hw.GPUs.Count -eq 0) {
    Write-Host "   GPU               : No dedicated GPU found; not needed (speech runs in the cloud)." -ForegroundColor Yellow
} else {
    foreach ($g in $hw.GPUs) {
        Write-Host "   GPU               : $g" -ForegroundColor White
    }
}

if ($hw.HasIntelArc) {
    Write-Host "   Intel Arc / XPU   : Detected (not needed: Gemini runs in the cloud)." -ForegroundColor Green
}

if ($hw.HasNvidia) {
    Write-Host "   NVIDIA GPU (CUDA) : Detected (not needed: Gemini runs in the cloud)." -ForegroundColor Green
}

if ($hw.NPUs.Count -gt 0) {
    foreach ($n in $hw.NPUs) {
        Write-Host "   NPU               : $n" -ForegroundColor Green
    }
} else {
    Write-Host "   NPU               : None detected" -ForegroundColor Gray
}

Write-Host ""
Write-Host " Runtime Requirements:" -ForegroundColor Cyan

Write-Host "   VC++ 2015-2022    : " -NoNewline -ForegroundColor Gray
if ($hw.VCRedist) {
    Write-Host "Installed" -ForegroundColor Green
} else {
    Write-Host "Not found (only needed by some optional Python packages)" -ForegroundColor Yellow
}

Write-Host "   Python 3.10+      : " -NoNewline -ForegroundColor Gray
if ($hw.PythonPath) {
    $pyVer = (& $hw.PythonPath --version 2>&1)
    Write-Host "$pyVer ($($hw.PythonPath))" -ForegroundColor Green
} else {
    Write-Host "Not installed" -ForegroundColor Red
}

Write-Host ""
Write-Host ("-" * 64) -ForegroundColor Gray

# If Python is missing, offer interactive installation
if (-not $hw.PythonPath) {
    Write-Host ""
    Write-Host " [!] Python 3.12 was not found on your system." -ForegroundColor Yellow
    Write-Host "     Voice Dictation requires Python 3.10+ (Python 3.12 64-bit recommended)." -ForegroundColor Yellow
    Write-Host ""
    $reply = Read-Host " Would you like to install Python 3.12 automatically now? [Y/n]"
    if ($reply -eq "" -or $reply -match "^[yY]") {
        $installed = Install-Python312
        if ($installed) {
            $hw.PythonPath = $installed
        }
    }
}

Write-Host ""
