<#
Builds Taisync Telemetry.

    .\build.ps1              # exe + portable zip
    .\build.ps1 -Installer   # also compile the Inno Setup installer
    .\build.ps1 -SkipTest    # skip the post-build selftest

Output:
    dist\TaisyncTelemetry\TaisyncTelemetry.exe      the application
    dist_zip\TaisyncTelemetry-<ver>-portable.zip    unzip-and-run, no install
    dist_installer\TaisyncTelemetry-Setup-<ver>.exe installer  (-Installer)
#>
param(
    [switch]$Installer,
    [switch]$SkipTest
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$Version = "1.39"

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

Step "Checking dependencies"
python -c "import PySide6, requests, pyqtgraph, PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Missing packages. Installing from requirements.txt..." -ForegroundColor Yellow
    python -m pip install -r requirements.txt
    python -m pip install pyinstaller
}

Step "Building the application"
python -m PyInstaller --noconfirm --clean TaisyncTelemetry.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$exe = "dist\TaisyncTelemetry\TaisyncTelemetry.exe"
if (-not (Test-Path $exe)) { throw "Build produced no exe" }
$size = [math]::Round((Get-ChildItem "dist\TaisyncTelemetry" -Recurse |
                       Measure-Object -Property Length -Sum).Sum / 1MB, 1)
Write-Host "Built $exe ($size MB total)" -ForegroundColor Green

if (-not $SkipTest) {
    Step "Running selftest against the built exe"
    $p = Start-Process $exe -ArgumentList "--selftest" -PassThru -Wait
    $report = Join-Path $env:APPDATA "TaisyncTelemetry\selftest.txt"
    if (Test-Path $report) { Get-Content $report }
    if ($p.ExitCode -ne 0) { throw "Selftest FAILED - do not ship this build" }
    Write-Host "Selftest passed" -ForegroundColor Green
}

Step "Creating portable zip"
New-Item -ItemType Directory -Force -Path "dist_zip" | Out-Null
$zip = "dist_zip\TaisyncTelemetry-$Version-portable.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "dist\TaisyncTelemetry\*" -DestinationPath $zip -CompressionLevel Optimal
$zipMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "Created $zip ($zipMb MB)" -ForegroundColor Green

if ($Installer) {
    Step "Compiling installer"
    # $env:ISCC wins, then PATH, then the usual install locations. Inno Setup
    # is not always under Program Files -- on this machine it lives on D:.
    $iscc = $env:ISCC
    if (-not $iscc -or -not (Test-Path $iscc)) {
        $iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
    }
    if (-not $iscc) {
        $iscc = @(
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "D:\Inno Setup 6\ISCC.exe",
            "C:\Inno Setup 6\ISCC.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    }
    if (-not $iscc) {
        Write-Host "Inno Setup not found. Install it with:" -ForegroundColor Yellow
        Write-Host "    winget install JRSoftware.InnoSetup" -ForegroundColor Yellow
        throw "ISCC.exe not available"
    }
    & $iscc "installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
    $setup = "dist_installer\TaisyncTelemetry-Setup-$Version.exe"
    $setupMb = [math]::Round((Get-Item $setup).Length / 1MB, 1)
    Write-Host "Created $setup ($setupMb MB)" -ForegroundColor Green
}

Step "Done"
