<#
.SYNOPSIS
  One-click build of dist\bms2dtx.exe (PyInstaller onefile, windowed).

.DESCRIPTION
  * Creates .venv_build (--system-site-packages so numpy + tkinter are inherited; the base
    Python is never modified) and installs pyinstaller into it (needs network the first time).
  * Builds from bms2dtx.spec, then runs a headless smoke test of the exe.
  * ffmpeg.exe is NOT bundled; the exe finds it beside itself / in bin\ / on PATH / via the GUI picker.

.PARAMETER Python
  Base Python 3.12 interpreter (must have numpy + tkinter). Defaults to the DeepSeek Harness runtime
  if present, otherwise `python` on PATH.

.PARAMETER KeepVenv
  Keep .venv_build after building (default: kept; pass -CleanVenv to delete it).
#>
param(
    [string]$Python = "",
    [switch]$CleanVenv,
    [switch]$CleanBuildDir
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$projectVenv = Join-Path $root ".venv"
$projectVenvPy = Join-Path $projectVenv "Scripts\python.exe"
$usingProjectVenv = $false

if (-not $Python -and (Test-Path $projectVenvPy)) {
    $venv = $projectVenv
    $venvPy = $projectVenvPy
    $usingProjectVenv = $true
    Write-Host "Using project venv: $venvPy"
} else {
    if (-not $Python) {
        $harness = Join-Path $env:LOCALAPPDATA "Programs\DeepSeek Harness\resources\runtime\primary-runtime\dependencies\python\python.exe"
        if (Test-Path $harness) { $Python = $harness } else { $Python = "python" }
    }
    Write-Host "Base Python: $Python"

    $venv = Join-Path $root ".venv_build"
    $venvPy = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {
        & $Python -m venv --system-site-packages $venv
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
    }
}

& $venvPy -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $venvPy -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "pip install pyinstaller failed (network required)" }
}
& $venvPy -c "import tkinter"
if ($LASTEXITCODE -ne 0) { throw "tkinter must be available to the build venv" }
& $venvPy -c "import numpy, soundfile, sounddevice, soxr, tkinterdnd2" 2>$null
if ($LASTEXITCODE -ne 0) {
    $req = Join-Path $root "requirements.txt"
    if (Test-Path $req) {
        & $venvPy -m pip install -r $req
    } else {
        & $venvPy -m pip install numpy soundfile sounddevice soxr tkinterdnd2
    }
    if ($LASTEXITCODE -ne 0) { throw "pip install dependencies failed" }
}

& $venvPy -m PyInstaller --noconfirm --clean --distpath (Join-Path $root "dist") --workpath (Join-Path $root "build") (Join-Path $root "bms2dtx.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }

$exe = Join-Path $root "dist\bms2dtx.exe"
if (-not (Test-Path $exe)) { throw "dist\bms2dtx.exe was not produced" }

# Smoke test: the windowed exe has no console, so it writes its report to a file.
$report = Join-Path $env:TEMP "bms2dtx_smoke.txt"
if (Test-Path $report) { Remove-Item $report -Force }
$proc = Start-Process -FilePath $exe -ArgumentList @("--smoke-test", $report) -Wait -PassThru -WindowStyle Hidden
Write-Host "Smoke exit code: $($proc.ExitCode)"
if (Test-Path $report) { Get-Content $report | ForEach-Object { Write-Host "  $_" } }
if ($proc.ExitCode -ne 0) { throw "bms2dtx.exe smoke test failed" }

if ($CleanBuildDir -and (Test-Path (Join-Path $root "build"))) { Remove-Item (Join-Path $root "build") -Recurse -Force }
if ($CleanVenv -and (-not $usingProjectVenv) -and (Test-Path $venv)) { Remove-Item $venv -Recurse -Force }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "OK: $exe ($size MB)"
