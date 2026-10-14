param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$taskRoot = $PSScriptRoot
$taskBuildEnv = Join-Path $taskRoot "build/desktop-env"
$taskBuildPython = Join-Path $taskBuildEnv "Scripts/python.exe"
# An old build environment can outlive the Python installation that created it.
# Keep that directory intact and use a fresh ignored build directory if needed.
if (Test-Path -LiteralPath $taskBuildPython) {
    $taskBuildUsable = $false
    try {
        & $taskBuildPython -c "import sys" 2>$null
        $taskBuildUsable = $LASTEXITCODE -eq 0
    } catch { }
    if (-not $taskBuildUsable) {
        $taskBuildEnv = Join-Path $taskRoot "build/desktop-env-recovery"
        $taskBuildPython = Join-Path $taskBuildEnv "Scripts/python.exe"
    }
}
Push-Location -LiteralPath $taskRoot
try {
    if (-not (Test-Path -LiteralPath $taskBuildPython)) {
        & $Python -m venv $taskBuildEnv
        if ($LASTEXITCODE -ne 0) { throw "Could not create the build environment." }
    }
    & $taskBuildPython -m pip install --disable-pip-version-check --no-cache-dir -r requirements-desktop-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Could not install packaging dependencies." }
    & $taskBuildPython -m PyInstaller --noconfirm --onefile --windowed --name GGALDesk --paths src --specpath build --distpath dist --workpath build/desktop --hidden-import exchange_calendars.exchange_calendar_xbue --collect-data exchange_calendars --collect-data tzdata desktop_launcher.py
    if ($LASTEXITCODE -ne 0) { throw "Could not build GGALDesk.exe." }
    Write-Host "Ready: $taskRoot/dist/GGALDesk.exe"
} finally {
    Pop-Location
}
